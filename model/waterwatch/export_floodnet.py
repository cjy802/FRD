"""Materialise FloodNet-Supervised_v1.0 into the shared 0/1-mask project layout.

FloodNet (DJI Mavic Pro post-Harvey aerial, 4000x3000 JPEG + 10-class index
PNG labels per DATASET-VERSION-NOTE-v1.0) is re-encoded for both training
stacks: water = class 5 (Water) plus class 3 (Road-flooded), everything else
background; images are downscaled to a 1280-long-side JPEG so dataloaders
decode them fast. The official Train/Val/Test split (1445/450/448) is kept
untouched.
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

import cv2
import numpy as np

WATER_CLASSES = (5, 3)  # Water, Road-flooded
ROOT = "FloodNet-Supervised_v1.0"


def water_mask_from_index(index_image: np.ndarray) -> np.ndarray:
    return np.isin(index_image, WATER_CLASSES).astype(np.uint8)


def _paired_names(zf: zipfile.ZipFile, split: str) -> list[tuple[str, str, str]]:
    org_dir = f"{ROOT}/{split}/{split}-org-img/"
    lab_dir = f"{ROOT}/{split}/{split}-label-img/"
    images = {Path(n).stem: n for n in zf.namelist()
              if n.startswith(org_dir) and n.lower().endswith((".jpg", ".jpeg")) and Path(n).stem}
    labels = {Path(n).stem.removesuffix("_lab"): n for n in zf.namelist()
              if n.startswith(lab_dir) and n.lower().endswith(".png") and Path(n).stem}
    stems = sorted(set(images) & set(labels))
    return [(stem, images[stem], labels[stem]) for stem in stems]


def export_split(zf: zipfile.ZipFile, split: str, output: Path, long_side: int) -> dict:
    images_out = output / split / "images"
    masks_out = output / split / "masks"
    images_out.mkdir(parents=True, exist_ok=True)
    masks_out.mkdir(parents=True, exist_ok=True)
    pairs = _paired_names(zf, split)
    water_pixels = total_pixels = 0
    skipped_no_water = 0
    for number, (stem, image_name, label_name) in enumerate(pairs, start=1):
        image = cv2.imdecode(np.frombuffer(zf.read(image_name), np.uint8), cv2.IMREAD_COLOR)
        label = cv2.imdecode(np.frombuffer(zf.read(label_name), np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None or label is None:
            raise ValueError(f"解码失败：{image_name} / {label_name}")
        height, width = image.shape[:2]
        scale = min(1.0, long_side / max(height, width))
        if scale < 1.0:
            image = cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
            label = cv2.resize(label, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_NEAREST)
        mask = water_mask_from_index(label)
        if not mask.any():
            skipped_no_water += 1
        cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tofile(str(images_out / f"{stem}.jpg"))
        cv2.imencode(".png", mask)[1].tofile(str(masks_out / f"{stem}.png"))
        water_pixels += int(mask.sum())
        total_pixels += mask.size
        if number % 200 == 0:
            print(f"{split}: {number}/{len(pairs)}", flush=True)
    return {"split": split, "pairs": len(pairs), "frames_without_water": skipped_no_water,
            "water_fraction": water_pixels / total_pixels if total_pixels else 0.0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("D:/DATASET/FloodNet.zip"))
    parser.add_argument("--output", type=Path, default=Path("data/floodnet"))
    parser.add_argument("--long-side", type=int, default=1280)
    args = parser.parse_args()
    if not args.archive.is_file():
        raise SystemExit(f"FloodNet 压缩包缺失：{args.archive}")
    splits = {}
    with zipfile.ZipFile(args.archive) as zf:
        for split in ("train", "val", "test"):
            splits[split] = export_split(zf, split, args.output.resolve(), args.long_side)
            print(json.dumps(splits[split], ensure_ascii=False), flush=True)
    (args.output / "export_report.json").write_text(
        json.dumps({"source": str(args.archive), "water_classes": WATER_CLASSES,
                    "long_side": args.long_side, "splits": splits}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
