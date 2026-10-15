"""Materialise the Caltech aerial RGB-T ZIP into the DELIVER (CMNeXt) training layout.

Reads data/external/labeled_rgbt_pairs.zip plus the official split text files and
writes data/deliver/CaltechRGBT/{rgb,ther,labels}/<mask-stem>.* with train/val/test.txt.

Label mapping: source 0/1 (unknown/background) -> 255 ignore; 2..11 -> 0..9
(water is source 9 -> index 7). Thermal is a 16-bit relative signal; each image's
valid (>0) pixels are percentile-normalised to 1..255 uint8 so that 0 remains
exactly the zero-padding mark. Label pixels under thermal padding are set to 255,
matching the support used by the RandomForest baseline for comparability.
"""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

import numpy as np
from PIL import Image

from .reproduce_caltech_rgbt import EXPECTED_ARCHIVE_MD5, hash_file, official_pairs, zip_member_index


CLASS_NAMES = ["bare ground", "boulders", "human-made structures", "road",
               "shrubs", "trees", "sky", "water", "vehicles", "person"]
WATER_INDEX = 7  # source label 9


def _decode_label(raw: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(raw)) as image:
        mask = np.asarray(image)
    if mask.ndim != 2 or not np.issubdtype(mask.dtype, np.integer):
        raise ValueError("Caltech mask must be a single-channel integer image")
    values = set(int(value) for value in np.unique(mask))
    if not values <= set(range(12)):
        raise ValueError(f"unexpected Caltech label values: {sorted(values)}")
    mapped = np.full(mask.shape, 255, dtype=np.uint8)
    for source in range(2, 12):
        mapped[mask == source] = source - 2
    return mapped


def _thermal_to_uint8(raw: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(raw)) as image:
        thermal = np.asarray(image).astype(np.int64)
    if thermal.ndim != 2:
        raise ValueError("thermal16 member must be single-channel")
    valid = thermal > 0
    if not valid.any():
        raise ValueError("thermal image has no non-zero pixels")
    low, high = np.percentile(thermal[valid], [1, 99])
    spread = max(float(high - low), 1.0)
    scaled = np.clip((thermal - low) / spread, 0.0, 1.0)
    out = np.where(valid, np.round(scaled * 254 + 1), 0).astype(np.uint8)
    return out


def export(archive_path: Path, splits_dir: Path, output: Path) -> dict:
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"Caltech ZIP missing: {archive_path}")
    archive_md5 = hash_file(archive_path, "md5")
    if archive_md5 != EXPECTED_ARCHIVE_MD5:
        raise ValueError(f"archive MD5 {archive_md5} does not match official checksum")
    splits, _ = official_pairs(splits_dir.resolve())
    output = output.resolve()
    for name in ("rgb", "ther", "labels"):
        (output / name).mkdir(parents=True, exist_ok=True)
    split_names: dict[str, list[str]] = {}
    label_counts: Counter[int] = Counter()
    thermal_zero_label_pixels = 0
    sizes: Counter[str] = Counter()
    with zipfile.ZipFile(archive_path) as archive:
        members = zip_member_index(archive)
        for split, pairs in splits.items():
            stems: list[str] = []
            for number, pair in enumerate(pairs, start=1):
                stem = PurePosixPath(pair.mask).stem
                color = archive.read(members[pair.rgb])
                thermal = _thermal_to_uint8(archive.read(members[pair.thermal]))
                label = _decode_label(archive.read(members[pair.mask]))
                if thermal.shape != label.shape:
                    raise ValueError(f"shape mismatch for {stem}")
                with Image.open(io.BytesIO(color)) as image:
                    rgb_size = image.size
                if (rgb_size[1], rgb_size[0]) != label.shape:
                    raise ValueError(f"RGB/mask size mismatch for {stem}: {rgb_size} vs {label.shape}")
                sizes[f"{rgb_size[0]}x{rgb_size[1]}"] += 1
                padding = thermal == 0
                thermal_zero_label_pixels += int((padding & (label != 255)).sum())
                label[padding] = 255
                label_counts.update(int(value) for value in np.unique(label) if value != 255)
                (output / "rgb" / f"{stem}.jpg").write_bytes(color)
                Image.fromarray(thermal, mode="L").save(output / "ther" / f"{stem}.png")
                Image.fromarray(label, mode="L").save(output / "labels" / f"{stem}.png")
                stems.append(stem)
                if number % 250 == 0:
                    print(f"[{split}] exported {number}/{len(pairs)}", flush=True)
            (output / f"{split}.txt").write_text("\n".join(stems) + "\n", encoding="utf-8")
            split_names[split] = stems
    overlap = set(split_names["train"]) & set(split_names["test"]) | set(split_names["train"]) & set(split_names["val"]) | set(split_names["val"]) & set(split_names["test"])
    if overlap:
        raise ValueError(f"split stems overlap: {sorted(overlap)[:3]}")
    report = {
        "archive": str(archive_path),
        "archive_md5": archive_md5,
        "output": str(output),
        "split_counts": {name: len(stems) for name, stems in split_names.items()},
        "image_sizes": dict(sizes),
        "label_value_counts_after_mapping": {str(key): value for key, value in sorted(label_counts.items())},
        "class_names": CLASS_NAMES,
        "water_index": WATER_INDEX,
        "label_pixels_under_thermal_padding_set_to_ignore": thermal_zero_label_pixels,
        "notes": [
            "source labels 0/1 -> 255 ignore; 2..11 -> 0..9 (water 9 -> 7)",
            "thermal normalised per image to 1..255; 0 marks stereo zero-padding",
            "labels under thermal padding are ignored for train/eval support parity with the RF baseline",
        ],
    }
    (output / "export_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/external/labeled_rgbt_pairs.zip"))
    parser.add_argument("--splits-dir", type=Path, default=Path("data/external/caltech_splits"))
    parser.add_argument("--output", type=Path, default=Path("data/deliver/CaltechRGBT"))
    args = parser.parse_args()
    report = export(args.archive, args.splits_dir, args.output)
    print(json.dumps({key: report[key] for key in ("split_counts", "image_sizes", "water_index")}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
