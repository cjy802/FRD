"""Fine-tune the ABCDWaveNet ponding model on locally labelled photos.

Input layout matches the ABCD dataloader: <data>/images plus <data>/masks where
masks are 0/1 PNGs (e.g. produced from labelme via `mask-from-labelme --task
shoreline`, or by the prelabel workflow). Starts from the Puddle-1000 checkpoint
(transfer learning), trains with a low learning rate, reports per-epoch
validation water IoU, keeps the best checkpoint, and optionally exports ONNX
plus a registry.json so demo_watch / the web panel can point at the new model
via --model-dir.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, random_split

from .download_paper_code import ABCD_COMMIT

IMAGE_SIZE = 256


def _load_model(repo: str, checkpoint: Path) -> torch.nn.Module:
    from model import UNet
    state = torch.load(checkpoint, map_location="cpu")
    if isinstance(state, dict) and "mask_values" in state:
        del state["mask_values"]
    model = UNet(n_channels=3, n_classes=2, bilinear=True)
    model.load_state_dict(state)
    return model


def _dice_loss(logits: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    probability = torch.softmax(logits, dim=1)[:, 1]
    intersection = torch.sum(probability * target, dim=(1, 2))
    union = torch.sum(probability + target, dim=(1, 2))
    return 1.0 - torch.mean((2 * intersection + 1) / (union + 1))


def _water_iou(model: torch.nn.Module, loader: DataLoader, device: torch.device) -> float:
    model.eval()
    intersection = union = 0
    for batch in loader:
        images = batch["image"].to(device)
        truth = batch["mask"].to(device)
        logits = model(images)
        prediction = logits.argmax(dim=1)
        intersection += int(((prediction == 1) & (truth == 1)).sum())
        union += int(((prediction == 1) | (truth == 1)).sum())
    return intersection / union if union else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--third-party", type=Path, default=Path("third_party"))
    parser.add_argument("--data", type=Path, required=True, help="folder with images/ and masks/ (0/1 masks)")
    parser.add_argument("--base-checkpoint", type=Path, default=Path("output/abcdwavenet_puddle1000/UNet_train_best_0.7751.pth"))
    parser.add_argument("--val-fraction", type=float, default=0.2, help="held-out fraction when no val/ split exists")
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--lr", type=float, default=2e-5, help="low on purpose; the base model already knows generic puddles")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=Path("output/abcdwavenet_local"))
    parser.add_argument("--export-dir", type=Path, default=None, help="also export ONNX + registry.json here for demo_watch --model-dir")
    args = parser.parse_args()

    repo = str((args.third_party / "ABCDWaveNet").resolve())
    if not Path(repo, ".git").is_dir():
        raise SystemExit("third_party/ABCDWaveNet 缺失；先运行 python -m waterwatch.download_paper_code")
    if repo not in sys.path:
        sys.path.insert(0, repo)
    import subprocess
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    if head != ABCD_COMMIT:
        raise SystemExit(f"ABCDWaveNet HEAD {head} 不是固定 commit {ABCD_COMMIT}")
    if not args.base_checkpoint.is_file():
        raise SystemExit(f"基础检查点缺失：{args.base_checkpoint}")
    images_dir = args.data / "images"
    masks_dir = args.data / "masks"
    if not images_dir.is_dir() or not masks_dir.is_dir():
        raise SystemExit(f"{args.data} 需要包含 images/ 和 masks/")

    from utils.data_loading import BasicDataset
    full = BasicDataset(str(images_dir), str(masks_dir), scale=1.0)
    val_dir = args.data / "val"
    if (val_dir / "images").is_dir():
        train_set = full
        val_set = BasicDataset(str(val_dir / "images"), str(val_dir / "masks"), scale=1.0)
        split_note = str(val_dir)
    else:
        val_count = max(1, int(len(full) * args.val_fraction))
        train_count = len(full) - val_count
        generator = torch.Generator().manual_seed(args.seed)
        train_set, val_set = random_split(full, [train_count, val_count], generator=generator)
        split_note = f"random {train_count}/{val_count} (seed {args.seed})"
    train_loader = DataLoader(train_set, batch_size=args.batch_size, shuffle=True, num_workers=0, pin_memory=True, drop_last=len(train_set) >= args.batch_size)
    val_loader = DataLoader(val_set, batch_size=args.batch_size, shuffle=False, num_workers=0)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = _load_model(repo, args.base_checkpoint.resolve()).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scaler = torch.amp.GradScaler("cuda", enabled=False)  # fp32: wavelet ops NaN in fp16

    args.output.mkdir(parents=True, exist_ok=True)
    best_iou = -1.0
    best_path = None
    started = time.perf_counter()
    print(json.dumps({"base": str(args.base_checkpoint), "train_images": len(train_set), "val_images": len(val_set),
                      "split": split_note, "epochs": args.epochs, "lr": args.lr, "device": str(device)}, ensure_ascii=False), flush=True)
    for epoch in range(1, args.epochs + 1):
        model.train()
        epoch_loss = 0.0
        for batch in train_loader:
            images = batch["image"].to(device)
            truth = batch["mask"].to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(images)
            loss = F.cross_entropy(logits, truth) + _dice_loss(logits, (truth == 1).float())
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            epoch_loss += float(loss)
        iou = _water_iou(model, val_loader, device)
        marker = ""
        if iou > best_iou:
            best_iou = iou
            best_path = args.output / f"abcdwavenet_local_best_{iou:.4f}.pth"
            torch.save(model.state_dict(), best_path)
            previous = sorted(args.output.glob("abcdwavenet_local_best_*.pth"))
            for old in previous:
                if old != best_path:
                    old.unlink()
            marker = " *best*"
        print(f"epoch {epoch}/{args.epochs} loss={epoch_loss / max(len(train_loader), 1):.4f} val_water_iou={iou:.4f}{marker}", flush=True)

    summary = {"best_val_water_iou": best_iou, "checkpoint": str(best_path),
               "base_checkpoint": str(args.base_checkpoint), "elapsed_seconds": round(time.perf_counter() - started, 1)}
    if args.export_dir and best_path:
        from .export_onnx import export_abcdwavenet
        export = export_abcdwavenet(args.third_party.resolve(), best_path.resolve(), (args.export_dir / "abcdwavenet_local.onnx").resolve())
        registry = {
            "default_model": "abcdwavenet_local.onnx",
            "task": "ponding",
            "water_class": 1,
            "input_size": 256,
            "normalize": "div255",
            "training_source": f"local fine-tune over {summary['base_checkpoint']}, {len(train_set)} train images, best val IoU {best_iou:.4f}",
            "water_iou_on_val": best_iou,
            "fine_tuned_at": datetime.now(timezone.utc).isoformat(),
            "notes": "在本 地照片上微调的积水模型；推理用法与基础模型一致。",
        }
        args.export_dir.mkdir(parents=True, exist_ok=True)
        (args.export_dir / "registry.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["export"] = {"onnx": export["onnx"], "max_abs_logit_deviation": export["verification"]["max_abs_logit_deviation"],
                             "registry": str(args.export_dir / "registry.json")}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
