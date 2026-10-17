"""Evaluate a trained ABCDWaveNet checkpoint with paper-comparable pixel metrics.

Loads the official UNet (ABCDWaveNet) from the patched third-party repo, resizes
test images and masks to 256x256 exactly like the training dataloader, and reports
water IoU / Dice / precision / recall, the confusion matrix, per-image IoU spread,
example overlays and report.json. The paper reports IoU on the puddle class.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image

IMAGE_SIZE = 256
WATER_VALUES = (1, 255)  # Puddle-1000 masks are 0/1; Carvana-style masks are 0/255


def load_model(repo: Path, checkpoint: Path) -> torch.nn.Module:
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from model import UNet
    state = torch.load(checkpoint, map_location="cpu")
    if isinstance(state, dict) and "mask_values" in state:
        del state["mask_values"]
    model = UNet(n_channels=3, n_classes=2, bilinear=True)
    model.load_state_dict(state)
    model.eval()
    return model


def load_image(path: Path, is_mask: bool) -> np.ndarray:
    image = Image.open(path)
    image = image.convert("L" if is_mask else "RGB")
    image = image.resize((IMAGE_SIZE, IMAGE_SIZE), resample=Image.NEAREST if is_mask else Image.BICUBIC)
    array = np.asarray(image)
    if is_mask:
        return np.isin(array, WATER_VALUES).astype(np.int64)
    tensor = array.transpose((2, 0, 1)).astype(np.float32) / 255.0
    return tensor


@torch.inference_mode()
def predict(model: torch.nn.Module, tensor: np.ndarray, device: torch.device) -> np.ndarray:
    batch = torch.from_numpy(tensor.copy()).unsqueeze(0).to(device, dtype=torch.float32, memory_format=torch.channels_last)
    logits = model(batch)
    if logits.shape[1] == 1:
        prediction = (torch.sigmoid(logits.squeeze(1)) > 0.5).long()[0]
    else:
        prediction = logits.argmax(dim=1)[0]
    return prediction.cpu().numpy().astype(np.int64)


def main() -> None:
    global IMAGE_SIZE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--third-party", type=Path, default=Path("third_party"))
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True, help="directory with images/ and masks/")
    parser.add_argument("--output", type=Path, default=Path("output/reproduction_abcdwavenet"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--image-size", type=int, default=IMAGE_SIZE,
                        help="evaluation resolution; the UNet is fully convolutional so this may differ from training")
    args = parser.parse_args()
    IMAGE_SIZE = args.image_size

    repo = (args.third_party / "ABCDWaveNet").resolve()
    dataset = args.dataset.resolve()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = load_model(repo, args.checkpoint.resolve()).to(device)

    stems = sorted(path.stem for path in (dataset / "images").iterdir() if path.is_file() and not path.name.startswith("."))
    stems = [stem for stem in stems if (dataset / "masks" / f"{stem}.png").exists() or (dataset / "masks" / f"{stem}.jpg").exists()]
    if args.limit > 0:
        stems = stems[:args.limit]
    if not stems:
        raise FileNotFoundError(f"no paired images/masks under {dataset}")

    confusion = np.zeros((2, 2), dtype=np.int64)
    per_image = []
    overlays = []
    started = time.perf_counter()
    for number, stem in enumerate(stems, start=1):
        image_path = next((dataset / "images").glob(f"{stem}.*"))
        mask_path = next((dataset / "masks").glob(f"{stem}.*"))
        tensor = load_image(image_path, is_mask=False)
        truth = load_image(mask_path, is_mask=True)
        prediction = predict(model, tensor, device)
        confusion += np.bincount(truth.ravel() * 2 + prediction.ravel(), minlength=4).reshape(2, 2)
        tp = int(((truth == 1) & (prediction == 1)).sum())
        union = int(((truth == 1) | (prediction == 1)).sum())
        entry = {"stem": stem, "water_iou": tp / union if union else None,
                 "water_truth_fraction": float((truth == 1).mean())}
        per_image.append(entry)
        if len(overlays) < 5 and (truth == 1).any():
            rgb = (tensor.transpose(1, 2, 0) * 255).astype(np.uint8)
            mask_rgb = rgb.copy()
            mask_rgb[prediction == 1] = (30, 110, 255)
            overlays.append((stem, cv2.addWeighted(rgb, 0.6, mask_rgb, 0.4, 0)))
        if number % 50 == 0:
            print(f"scored {number}/{len(stems)}", flush=True)

    tp, fp, fn = float(confusion[1, 1]), float(confusion[0, 1]), float(confusion[1, 0])
    water = {
        "iou": tp / (tp + fp + fn) if tp + fp + fn else None,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "dice": 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None,
    }
    ious = [entry["water_iou"] for entry in per_image if entry["water_iou"] is not None]

    args.output.mkdir(parents=True, exist_ok=True)
    for stem, image in overlays:
        cv2.imwrite(str(args.output / f"overlay_{stem}.png"), cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    report = {
        "task": "road ponding segmentation, ABCDWaveNet (official implementation)",
        "checkpoint": str(args.checkpoint.resolve()),
        "dataset": str(dataset),
        "image_size": IMAGE_SIZE,
        "device": str(device),
        "images": len(stems),
        "evaluated_pixels": int(confusion.sum()),
        "water_metrics": water,
        "confusion_rows_true_columns_predicted_background_water": confusion.tolist(),
        "per_image_water_iou": {
            "median": float(np.median(ious)) if ious else None,
            "min": float(min(ious)) if ious else None,
            "max": float(max(ious)) if ious else None,
        },
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"water_iou": water["iou"], "water_dice": water["dice"], "images": len(stems),
                      "report": str(args.output / "report.json")}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
