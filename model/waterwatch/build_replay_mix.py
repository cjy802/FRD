"""Assemble the four-domain replay-mix training set (Puddle + FRED + FloodNet + local).

Experience-replay mixing: sample image/mask pairs from each domain's existing
0/1-mask layout into one flat training folder so a single fine-tune sees all
domains at once and cannot sequentially forget the earlier ones (the
sequential fine-tunes in REPRODUCTION.md section 10 forgot FRED
catastrophically). A balanced mini-val (val/) is built for checkpoint
selection; the official held-out sets (Puddle val, FRED val, FloodNet test)
stay untouched for final scoring.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

from .yolo_seg_baseline import load_mask, paired_stems


def _load_mask_any(path: Path) -> np.ndarray:
    if path.suffix == ".npy":
        return (np.load(path) > 0).astype(np.uint8)
    return load_mask(path)


def sample_stems(stems: list[str], k: int, rng: random.Random) -> list[str]:
    if k <= 0 or k >= len(stems):
        return list(stems)
    return sorted(rng.sample(stems, k))


def build_mix(sources: dict, output: Path, seed: int = 42, val_per_domain: int = 0) -> dict:
    """sources: {name: {"images": Path, "masks": Path, "prefix": str,
    "train_k": int or 0 (all), "val": bool, "val_k": int or None (val_per_domain),
    "only_prefix": str or None}}; val sources may hold .npy masks."""
    rng = random.Random(seed)
    report = {"seed": seed, "domains": {}}
    for name, spec in sources.items():
        pairs = paired_stems(spec["images"].resolve(), spec["masks"].resolve())
        only_prefix = spec.get("only_prefix")
        stems = [stem for stem, _, _ in pairs if only_prefix is None or stem.startswith(only_prefix)]
        by_stem = {stem: (image_path, mask_path) for stem, image_path, mask_path in pairs if stem in set(stems)}
        train_stems = sample_stems(stems, spec.get("train_k", 0), rng) if spec.get("train", True) else []
        val_stems = sample_stems(stems, spec.get("val_k") or val_per_domain, rng) if spec.get("val") else []
        stats = {"train": 0, "val": 0, "train_water_fraction": 0.0, "val_water_fraction": 0.0}
        for split, chosen in (("train", train_stems), ("val", val_stems)):
            if not chosen:
                continue
            images_out = output / split / "images"
            masks_out = output / split / "masks"
            images_out.mkdir(parents=True, exist_ok=True)
            masks_out.mkdir(parents=True, exist_ok=True)
            prefix = spec["prefix"]
            water = total = 0
            for stem in chosen:
                image_path, mask_path = by_stem[stem]
                mask = _load_mask_any(mask_path)
                water += int(mask.sum())
                total += mask.size
                shutil.copy2(image_path, images_out / f"{prefix}_{stem}{image_path.suffix.lower()}")
                Image.fromarray(mask, mode="L").save(masks_out / f"{prefix}_{stem}.png")
            stats[split] = len(chosen)
            stats[f"{split}_water_fraction"] = round(water / total, 4) if total else 0.0
        report["domains"][name] = stats
        print(json.dumps({name: stats}, ensure_ascii=False), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/replay_mix"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--puddle-k", type=int, default=300)
    parser.add_argument("--fred-k", type=int, default=0, help="0 = all 812")
    parser.add_argument("--floodnet-k", type=int, default=500)
    parser.add_argument("--val-per-domain", type=int, default=60)
    args = parser.parse_args()
    sources = {
        "puddle": {"images": Path("data/abcd/puddle1000/train/images"),
                   "masks": Path("data/abcd/puddle1000/train/masks"),
                   "prefix": "puddle", "train_k": args.puddle_k,
                   "val": True, "val_k": args.val_per_domain},
        "fred": {"images": Path("data/fred_ft/images"), "masks": Path("data/fred_ft/masks"),
                 "prefix": "fred", "train_k": args.fred_k,
                 "val": True, "val_k": args.val_per_domain},
        "floodnet": {"images": Path("data/floodnet/train/images"), "masks": Path("data/floodnet/train/masks"),
                     "prefix": "floodnet", "train_k": args.floodnet_k,
                     "val": True, "val_k": args.val_per_domain},
        # oversampled local copies (incl. flips) produced by seed_local_labels in data/local_mix;
        # only local_* pairs — the other 812 files are byte-identical FRED copies
        "local": {"images": Path("data/local_mix/images"), "masks": Path("data/local_mix/masks"),
                  "prefix": "local", "train_k": 0, "val": False, "only_prefix": "local_"},
    }
    if args.val_per_domain > 0:
        sources["localval"] = {"images": Path("data/local_seed/images"), "masks": Path("data/local_seed/masks"),
                               "prefix": "vlocal", "train": False, "val": True}
    for key, spec in sources.items():
        if not spec["images"].is_dir() or not spec["masks"].is_dir():
            raise SystemExit(f"{key} 数据缺失：{spec['images']} / {spec['masks']}")
    if (args.output / "train" / "images").is_dir():
        raise SystemExit(f"{args.output}/train 已存在；换 --output 或手动清理后重跑")
    report = build_mix(sources, args.output.resolve(), seed=args.seed, val_per_domain=args.val_per_domain)
    report["train_total"] = sum(d["train"] for d in report["domains"].values())
    report["val_total"] = sum(d["val"] for d in report["domains"].values())
    (args.output / "build_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
    print(json.dumps({"train_total": report["train_total"], "val_total": report["val_total"],
                      "output": str(args.output.resolve())}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
