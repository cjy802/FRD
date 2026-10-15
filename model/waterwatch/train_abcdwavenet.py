"""Launch ABCDWaveNet training (official code, patched CLI) on a prepared dataset.

The dataset directory must contain images/ and masks/ with matching file names
(grayscale masks, 0 = background, 255 = water), as produced by the export script.
Training and validation directories are separate; the wrapper only verifies the
layout, pins provenance, and runs third_party/ABCDWaveNet/train.py in a subprocess.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .download_paper_code import ABCD_COMMIT

MASK_VALUES = {0, 255}


def _verify_dir(root: Path, name: str) -> dict:
    images = root / "images"
    masks = root / "masks"
    for folder in (images, masks):
        if not folder.is_dir():
            raise FileNotFoundError(f"{name} dataset missing {folder}")
    image_names = {p.stem for p in images.iterdir() if p.is_file() and not p.name.startswith(".")}
    mask_names = {p.stem for p in masks.iterdir() if p.is_file() and not p.name.startswith(".")}
    if not image_names:
        raise FileNotFoundError(f"{name}/images is empty")
    missing = sorted(image_names - mask_names)
    if missing:
        raise FileNotFoundError(f"{name}: masks missing for {len(missing)} images, e.g. {missing[:3]}")
    return {"root": str(root), "images": len(image_names)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--third-party", type=Path, default=Path("third_party"))
    parser.add_argument("--train-dir", type=Path, required=True)
    parser.add_argument("--val-dir", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--amp", action="store_true", default=True)
    parser.add_argument("--checkpoint-dir", type=Path, default=Path("output/abcdwavenet_checkpoints"))
    parser.add_argument("--log", type=Path, default=Path("output/abcdwavenet_train.log"))
    args = parser.parse_args()

    repo = (args.third_party / "ABCDWaveNet").resolve()
    if not (repo / ".git").is_dir():
        raise FileNotFoundError("third_party/ABCDWaveNet missing; run python -m waterwatch.download_paper_code")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    if head != ABCD_COMMIT:
        raise ValueError(f"ABCDWaveNet HEAD {head} is not pinned commit {ABCD_COMMIT}")
    if "apply_runtime_options" not in (repo / "train.py").read_text(encoding="utf-8"):
        raise ValueError("ABCDWaveNet train.py is not patched; run python -m waterwatch.download_paper_code")

    train_info = _verify_dir(args.train_dir.resolve(), "train")
    val_info = _verify_dir(args.val_dir.resolve(), "val")
    checkpoint_dir = args.checkpoint_dir.resolve()
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable, "train.py",
        "--train-dir", train_info["root"],
        "--val-dir", val_info["root"],
        "--checkpoint-dir", str(checkpoint_dir),
        "--output-dir", str(checkpoint_dir.parent / "abcdwavenet_output"),
        "--epochs", str(args.epochs),
        "--batch-size", str(args.batch_size),
        "--lr", str(args.lr),
        "--classes", "2",
    ]
    if args.amp:
        command.append("--amp")
    environment = dict(os.environ, PYTHONUNBUFFERED="1")
    args.log.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    print(json.dumps({"repo_commit": head, "train": train_info, "val": val_info, "command": command}, ensure_ascii=False, indent=2), flush=True)
    with args.log.open("w", encoding="utf-8") as log_stream:
        process = subprocess.run(command, cwd=repo, env=environment, stdout=log_stream, stderr=subprocess.STDOUT)
    summary = {"returncode": process.returncode, "elapsed_seconds": round(time.perf_counter() - started, 1),
               "command": command, "log": str(args.log), "checkpoint_dir": str(checkpoint_dir)}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if process.returncode != 0:
        raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
