"""Materialise the downloaded Puddle-1000 / Foggy-Puddle folders for ABCDWaveNet.

The Quark archives ship per-split directories (images/ + masks/) with 0/1 grayscale
masks. This exporter copies only paired files into data/abcd/<name>/{images,masks},
drops browser-duplicate leftovers like '000001853(1).png', and records mask value
statistics. The night archive and the on/off-road subsets contain no masks and are
intentionally skipped (documented degradation, not silently ignored).
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

KNOWN_VARIANTS = {
    "puddle1000": {
        "train": "Puddle-1000 Dataset/Puddle-1000 Dataset_train",
        "val": "Puddle-1000 Dataset/Puddle-1000 Dataset_val",
    },
    "foggy": {
        "train": "Puddle-1000 Dataset_foggy3.2/Puddle-1000 Dataset_train_foggy3.2",
        "val": "Puddle-1000 Dataset_foggy3.2/Puddle-1000 Dataset_val_foggy3.2",
    },
}


def _paired(source: Path) -> list[tuple[Path, Path]]:
    images = {p.stem: p for p in (source / "images").iterdir() if p.is_file()}
    masks = {p.stem: p for p in (source / "masks").iterdir() if p.is_file()}
    shared = sorted(set(images) & set(masks))
    return [(images[stem], masks[stem]) for stem in shared]


def export_split(source: Path, target: Path) -> dict:
    pairs = _paired(source)
    if not pairs:
        raise ValueError(f"no paired images/masks under {source}")
    (target / "images").mkdir(parents=True, exist_ok=True)
    (target / "masks").mkdir(parents=True, exist_ok=True)
    dropped = sorted({p.stem for p in (source / "images").iterdir() if p.is_file()} -
                     {p.stem for p in (source / "masks").iterdir() if p.is_file()})
    mask_values: Counter[int] = Counter()
    water_pixels = 0
    total_pixels = 0
    sizes: Counter[str] = Counter()
    for image_path, mask_path in pairs:
        shutil.copy2(image_path, target / "images" / image_path.name)
        shutil.copy2(mask_path, target / "masks" / mask_path.name)
        with Image.open(mask_path) as mask_image:
            mask = np.asarray(mask_image)
            mask_image_size = mask_image.size
        mask_values.update(int(value) for value in np.unique(mask))
        water_pixels += int((mask == 1).sum())
        total_pixels += int(mask.size)
        sizes[f"{mask_image_size[0]}x{mask_image_size[1]}"] += 1
    return {
        "source": str(source),
        "pairs": len(pairs),
        "images_without_masks_dropped": dropped,
        "mask_unique_values": {str(key): value for key, value in sorted(mask_values.items())},
        "water_pixel_fraction": water_pixels / total_pixels,
        "image_sizes": dict(sizes),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--output-root", type=Path, default=Path("data/abcd"))
    parser.add_argument("--variant", choices=tuple(KNOWN_VARIANTS), required=True)
    args = parser.parse_args()
    root = args.data_root.resolve()
    report = {"variant": args.variant, "source_root": str(root), "splits": {}}
    for split, relative in KNOWN_VARIANTS[args.variant].items():
        source = root / relative
        if not source.is_dir():
            raise FileNotFoundError(f"missing split directory: {source}")
        report["splits"][split] = export_split(source, args.output_root.resolve() / args.variant / split)
    report["notes"] = [
        "masks are 0/1 grayscale; the ABCDWaveNet dataloader maps sorted unique values to class indices",
        "night and on/off-road subsets ship without masks and are not exported",
    ]
    output_path = args.output_root.resolve() / args.variant / "export_report.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"variant": args.variant,
                      "splits": {name: info["pairs"] for name, info in report["splits"].items()},
                      "report": str(output_path)}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
