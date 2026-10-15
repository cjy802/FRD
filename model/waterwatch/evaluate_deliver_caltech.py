"""Final test evaluation of a trained CMNeXt on the exported Caltech RGB-T set.

Runs single-image full-resolution inference (padded to a multiple of 32), reports
the paper-style 10-class mIoU plus the water class metrics, and additionally the
binary water-vs-rest view that is directly comparable with the RandomForest
baseline in REPRODUCTION.md. Saves example overlays and report.json.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
import yaml
from torch import Tensor
from torchvision import io

CLASS_NAMES = ["bare ground", "boulders", "human-made structures", "road",
               "shrubs", "trees", "sky", "water", "vehicles", "person"]
PALETTE = np.array([[242, 216, 196], [89, 70, 54], [166, 166, 166], [82, 89, 90],
                    [155, 230, 0], [0, 138, 53], [0, 216, 245], [13, 127, 252],
                    [255, 249, 0], [254, 0, 170]], dtype=np.uint8)
WATER_INDEX = 7
IGNORE = 255


def load_model(deliver_root: Path, checkpoint: Path, modals: list[str]) -> torch.nn.Module:
    sys_path_root = str(deliver_root)
    import sys
    if sys_path_root not in sys.path:
        sys.path.insert(0, sys_path_root)
    from semseg.models import CMNeXt
    model = CMNeXt("CMNeXt-B2", len(CLASS_NAMES), modals)
    state = torch.load(checkpoint, map_location="cpu")
    if isinstance(state, dict) and "model_state_dict" in state:
        state = state["model_state_dict"]
    cleaned = {key.removeprefix("module."): value for key, value in state.items()}
    message = model.load_state_dict(cleaned, strict=True)
    model.eval()
    return model


@torch.inference_mode()
def predict(model: torch.nn.Module, sample: list[Tensor]) -> np.ndarray:
    height, width = sample[0].shape[2:]
    pad_h = (32 - height % 32) % 32
    pad_w = (32 - width % 32) % 32
    padded = [F.pad(x, (0, pad_w, 0, pad_h)) for x in sample]
    logits = model(padded)
    logits = logits[:, :, :height, :width]
    return logits.argmax(dim=1)[0].cpu().numpy().astype(np.uint8)


def iou_precision_recall(confusion: np.ndarray, index: int) -> dict:
    tp = float(confusion[index, index])
    fp = float(confusion[:, index].sum() - tp)
    fn = float(confusion[index, :].sum() - tp)
    return {
        "iou": tp / (tp + fp + fn) if tp + fp + fn else None,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
    }


def colorize(index_mask: np.ndarray) -> np.ndarray:
    out = np.zeros((*index_mask.shape, 3), dtype=np.uint8)
    for index in range(len(CLASS_NAMES)):
        out[index_mask == index] = PALETTE[index]
    return out


def overlay(rgb: np.ndarray, index_mask: np.ndarray) -> np.ndarray:
    colored = colorize(index_mask)
    return (0.6 * rgb + 0.4 * colored).astype(np.uint8)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--third-party", type=Path, default=Path("third_party"))
    parser.add_argument("--dataset", type=Path, default=Path("data/deliver/CaltechRGBT"))
    parser.add_argument("--config", type=Path, default=None, help="runtime config; defaults to configs/_runtime_caltech_rgbt.yaml")
    parser.add_argument("--checkpoint", type=Path, default=None, help="explicit .pth; otherwise latest best in the run output dir")
    parser.add_argument("--output", type=Path, default=Path("output/reproduction_caltech_cmnext"))
    parser.add_argument("--limit", type=int, default=0, help="evaluate only the first N test images (0 = all)")
    args = parser.parse_args()

    deliver_root = (args.third_party / "DELIVER").resolve()
    config_path = args.config or (deliver_root / "configs/_runtime_caltech_rgbt.yaml")
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    modals = cfg["DATASET"]["MODALS"]
    dataset_root = args.dataset.resolve()
    stems = [line.strip() for line in (dataset_root / "test.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit > 0:
        stems = stems[:args.limit]

    checkpoint = args.checkpoint
    if checkpoint is None:
        run_dir = Path(cfg["SAVE_DIR"], "CaltechRGBT_CMNeXt-B2_" + "".join(m[0] for m in modals))
        candidates = [p for p in (deliver_root / run_dir).glob("*.pth") if not p.name.endswith("_checkpoint.pth")]
        if not candidates:
            raise FileNotFoundError(f"no trained checkpoint under {deliver_root / run_dir}")

        def score(path: Path) -> float:
            stem = path.stem.rsplit("_", 1)[-1]
            try:
                return float(stem)
            except ValueError:
                return -1.0

        checkpoint = max(candidates, key=score)
    checkpoint = checkpoint.resolve()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = load_model(deliver_root, checkpoint, modals).to(device)
    mean = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)

    confusion = np.zeros((len(CLASS_NAMES), len(CLASS_NAMES)), dtype=np.int64)
    binary_confusion = np.zeros((2, 2), dtype=np.int64)
    per_image = []
    examples = []
    started = time.perf_counter()
    for number, stem in enumerate(stems, start=1):
        rgb = io.read_image(str(dataset_root / "rgb" / f"{stem}.jpg"))[:3]
        thermal = io.read_image(str(dataset_root / "ther" / f"{stem}.png"))
        if thermal.shape[0] == 1:
            thermal = thermal.repeat(3, 1, 1)
        label = io.read_image(str(dataset_root / "labels" / f"{stem}.png"))[0].numpy()
        sample = [(x.float() / 255.0).to(device) for x in (rgb, thermal)]
        sample[0] = (sample[0] - mean.to(device)) / std.to(device)
        prediction = predict(model, sample)
        valid = label != IGNORE
        truth = label[valid].astype(np.int64)
        found = prediction[valid].astype(np.int64)
        confusion += np.bincount(truth * len(CLASS_NAMES) + found, minlength=len(CLASS_NAMES) ** 2).reshape(len(CLASS_NAMES), -1)
        binary_truth = (truth == WATER_INDEX).astype(np.int64)
        binary_found = (found == WATER_INDEX).astype(np.int64)
        binary_confusion += np.bincount(binary_truth * 2 + binary_found, minlength=4).reshape(2, 2)
        entry = {"stem": stem, "valid_pixels": int(valid.sum()), "water_truth_pixels": int(binary_truth.sum())}
        tp = int(((truth == WATER_INDEX) & (found == WATER_INDEX)).sum())
        union = int(((truth == WATER_INDEX) | (found == WATER_INDEX)).sum())
        entry["water_iou"] = tp / union if union else None
        per_image.append(entry)
        rgb_np = rgb.permute(1, 2, 0).numpy()
        thermal_np = thermal.permute(1, 2, 0).numpy()
        examples.append((entry, rgb_np, thermal_np, label, prediction))
        if number % 25 == 0:
            print(f"scored {number}/{len(stems)}", flush=True)

    per_class = {name: iou_precision_recall(confusion, index) for index, name in enumerate(CLASS_NAMES)}
    for index, name in enumerate(CLASS_NAMES):
        per_class[name]["support_pixels"] = int(confusion[index, :].sum())
    binary_water = iou_precision_recall(binary_confusion, 1)
    water_ious = [e["water_iou"] for e in per_image if e["water_iou"] is not None]

    args.output.mkdir(parents=True, exist_ok=True)
    ranked = sorted((e for e in examples if e[0]["water_truth_pixels"] > 0), key=lambda item: item[0]["water_iou"] or 0.0)
    chosen = ranked[-3:] + ranked[:2]
    for entry, rgb_np, thermal_np, label, prediction in chosen:
        folder = args.output / "examples" / entry["stem"]
        folder.mkdir(parents=True, exist_ok=True)
        io.write_png(rgb_np.permute(2, 0, 1), str(folder / "input_rgb.png"))
        io.write_png(thermal_np.permute(2, 0, 1), str(folder / "input_thermal.png"))
        truth_vis = np.full(label.shape, IGNORE, dtype=np.uint8)
        truth_vis[...] = label
        io.write_png(torch.from_numpy(truth_vis).unsqueeze(0), str(folder / "truth.png"))
        io.write_png(torch.from_numpy(prediction).unsqueeze(0), str(folder / "prediction.png"))
        io.write_png(torch.from_numpy(overlay(rgb_np, prediction)).permute(2, 0, 1), str(folder / "overlay.png"))
        entry_path = folder / "water_iou.json"
        entry_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")

    report = {
        "task": "caltech aerial RGB-T water segmentation, CMNeXt-B2 (official DELIVER implementation)",
        "checkpoint": str(checkpoint),
        "config": str(config_path),
        "modals": modals,
        "device": str(device),
        "test_images": len(stems),
        "evaluated_pixels": int(confusion.sum()),
        "class_metrics": per_class,
        "miou_10class": float(np.mean([m["iou"] for m in per_class.values()])),
        "water_class_metrics": per_class["water"],
        "binary_water_vs_rest": {
            **binary_water,
            "confusion_rows_true_columns_predicted_nonwater_water": binary_confusion.tolist(),
            "note": "collapses the 10 classes to water vs rest on valid pixels; comparable with the RandomForest baseline",
        },
        "per_image_water_iou": {
            "median": float(np.median(water_ious)) if water_ious else None,
            "min": float(min(water_ious)) if water_ious else None,
            "max": float(max(water_ious)) if water_ious else None,
            "images_with_water": len(water_ious),
        },
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "checkpoint": report["checkpoint"],
        "test_images": report["test_images"],
        "miou_10class": round(report["miou_10class"], 4),
        "water_class_iou": round(report["water_class_metrics"]["iou"], 4),
        "binary_water_iou": round(binary_water["iou"], 4),
        "report": str(args.output / "report.json"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
