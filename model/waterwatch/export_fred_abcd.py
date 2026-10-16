"""Materialise the FRED flooded-road subset into the ABCD fine-tuning layout.

FRED (Flooded Road Environments Dataset, https://huggingface.co/datasets/CMalone-Jupiter/FRED,
CC BY-NC-SA 4.0) provides per-frame semantic PNGs where pixel value 1 is water
(verified against the official SDK's evaluate-predictions.py). This exporter pairs
front-imgs/front-labels by timestamp, writes 0/1 masks, and splits by whole
sequence so that validation locations are never seen in training. Unpaired
timestamps are dropped and counted.
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

WATER_VALUE = 1


def export_sequence(sequence_dir: Path, images_out: Path, masks_out: Path, prefix: str,
                    limit: int = 0) -> dict:
    imgs = sequence_dir / "front-imgs"
    labels = sequence_dir / "front-labels"
    if not imgs.is_dir() or not labels.is_dir():
        raise FileNotFoundError(f"{sequence_dir} 缺少 front-imgs/front-labels")
    stems = {p.stem for p in imgs.iterdir() if p.suffix.lower() == ".png"}
    label_stems = {p.stem for p in labels.iterdir() if p.suffix.lower() == ".png"}
    paired = sorted(stems & label_stems)
    dropped = len(stems) - len(paired)
    if limit:
        step = max(1, len(paired) // limit)
        paired = paired[::step][:limit]
    water_pixels = total_pixels = 0
    sizes: Counter[str] = Counter()
    for stem in paired:
        image_path = imgs / f"{stem}.png"
        with Image.open(image_path) as image_file:
            width, height = image_file.size
        shutil.copy2(image_path, images_out / f"{prefix}_{stem}.png")
        with Image.open(labels / f"{stem}.png") as label_image:
            label = np.asarray(label_image)
        # Official SDK alignment: plain NEAREST resize of labels to image size.
        if label.shape[:2] != (height, width):
            label = cv2.resize(label, (width, height), interpolation=cv2.INTER_NEAREST)
        mask = (label == WATER_VALUE).astype(np.uint8)
        Image.fromarray(mask, mode="L").save(masks_out / f"{prefix}_{stem}.png")
        water_pixels += int(mask.sum())
        total_pixels += mask.size
        sizes[f"{width}x{height}"] += 1
    return {"sequence": prefix, "pairs": len(paired), "unpaired_dropped": dropped,
            "water_fraction": water_pixels / total_pixels if total_pixels else 0.0,
            "sizes": dict(sizes)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fred-root", type=Path, default=Path("data/external/fred/flooded/KITTI-style"))
    parser.add_argument("--output", type=Path, default=Path("data/fred_ft"))
    parser.add_argument("--val-sequences", type=str, default="Holmview_20250820_130327",
                        help="comma-separated sequences held out entirely for validation")
    parser.add_argument("--exclude-sequences", type=str, default="Mount-Cotton_20241217_113410",
                        help="comma-separated sequences to skip (e.g. labels without any water class)")
    parser.add_argument("--train-per-sequence", type=int, default=0, help="cap training frames per sequence (0 = all)")
    parser.add_argument("--val-per-sequence", type=int, default=0)
    args = parser.parse_args()
    root = args.fred_root.resolve()
    if not root.is_dir():
        raise SystemExit(f"FRED 目录缺失：{root}（先下载 flooded/KITTI-style 子集）")
    val_names = {name.strip() for name in args.val_sequences.split(",") if name.strip()}
    excluded = {name.strip() for name in args.exclude_sequences.split(",") if name.strip()}
    output = args.output.resolve()
    for sub in ("images", "masks", "val/images", "val/masks"):
        (output / sub).mkdir(parents=True, exist_ok=True)
    report = {"source": str(root), "water_value": WATER_VALUE,
              "val_sequences": sorted(val_names), "excluded_sequences": sorted(excluded), "splits": {}}
    for sequence_dir in sorted(root.iterdir()):
        if not sequence_dir.is_dir() or sequence_dir.name in excluded:
            continue
        prefix = sequence_dir.name
        is_val = prefix in val_names
        limit = args.val_per_sequence if is_val else args.train_per_sequence
        images_out = output / ("val/images" if is_val else "images")
        masks_out = output / ("val/masks" if is_val else "masks")
        info = export_sequence(sequence_dir, images_out, masks_out, prefix, limit=limit)
        report["splits"][prefix] = info
        print(json.dumps(info, ensure_ascii=False), flush=True)
    (output / "export_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    train_pairs = sum(info["pairs"] for name, info in report["splits"].items() if name not in val_names)
    val_pairs = sum(info["pairs"] for name, info in report["splits"].items() if name in val_names)
    print(json.dumps({"train_pairs": train_pairs, "val_pairs": val_pairs, "output": str(output)},
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
