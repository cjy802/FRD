"""Launch CMNeXt-B2 training on the exported Caltech RGB-T dataset (DELIVER codebase).

Verifies the pinned third-party tree, exported dataset and pretrained weights, writes
a runtime config with the requested overrides, and runs tools/train_mm.py in a
subprocess (WORLD_SIZE=1, single GPU). The actual training runs for hours; logs go
to the DELIVER output directory and to --log.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .download_paper_code import DELIVER_COMMIT, MIT_B2_SHA256, _sha256

DEFAULT_IMAGE_SIZE = [480, 640]


def _verify(deliver_root: Path, dataset_root: Path, weights: Path, splits_dir: Path) -> dict:
    if not (deliver_root / ".git").is_dir():
        raise FileNotFoundError("third_party/DELIVER missing; run python -m waterwatch.download_paper_code")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=deliver_root, capture_output=True, text=True).stdout.strip()
    if head != DELIVER_COMMIT:
        raise ValueError(f"DELIVER HEAD {head} is not pinned commit {DELIVER_COMMIT}")
    if not (deliver_root / "semseg/datasets/caltech_rgbt.py").is_file():
        raise ValueError("DELIVER is not patched; run python -m waterwatch.download_paper_code")
    digest = _sha256(weights)
    if digest != MIT_B2_SHA256:
        raise ValueError(f"mit_b2.pth SHA256 {digest} does not match pinned value")
    counts = {}
    for split in ("train", "val", "test"):
        listing = dataset_root / f"{split}.txt"
        if not listing.is_file():
            raise FileNotFoundError(f"missing {listing}; run python -m waterwatch.export_caltech_deliver")
        counts[split] = len([line for line in listing.read_text(encoding="utf-8").splitlines() if line.strip()])
    if counts != {"train": 1731, "val": 272, "test": 279}:
        raise ValueError(f"unexpected official split sizes: {counts}")
    return {"deliver_commit": head, "mit_b2_sha256": digest, "split_counts": counts, "splits_dir": str(splits_dir)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--third-party", type=Path, default=Path("third_party"))
    parser.add_argument("--dataset", type=Path, default=Path("data/deliver/CaltechRGBT"))
    parser.add_argument("--splits-dir", type=Path, default=Path("data/external/caltech_splits"))
    parser.add_argument("--epochs", type=int, default=300)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--eval-batch-size", type=int, default=2)
    parser.add_argument("--image-size", type=int, nargs=2, default=DEFAULT_IMAGE_SIZE, metavar=("H", "W"))
    parser.add_argument("--eval-start", type=int, default=100)
    parser.add_argument("--eval-interval", type=int, default=5)
    parser.add_argument("--amp", action="store_true", default=True)
    parser.add_argument("--log", type=Path, default=Path("output/cmnext_caltech_train.log"))
    args = parser.parse_args()

    deliver_root = (args.third_party / "DELIVER").resolve()
    dataset_root = args.dataset.resolve()
    weights = deliver_root / "checkpoints/pretrained/segformer/mit_b2.pth"
    verified = _verify(deliver_root, dataset_root, weights, args.splits_dir.resolve())

    base_config = deliver_root / "configs/caltech_rgbt_b2.yaml"
    cfg = yaml.safe_load(base_config.read_text(encoding="utf-8"))
    cfg["DATASET"]["ROOT"] = str(dataset_root)
    cfg["TRAIN"].update({
        "IMAGE_SIZE": list(args.image_size),
        "BATCH_SIZE": args.batch_size,
        "EPOCHS": args.epochs,
        "EVAL_START": args.eval_start,
        "EVAL_INTERVAL": args.eval_interval,
        "AMP": bool(args.amp),
        "DDP": False,
    })
    cfg["EVAL"]["BATCH_SIZE"] = args.eval_batch_size
    cfg["EVAL"]["IMAGE_SIZE"] = list(args.image_size)
    runtime_config = deliver_root / "configs/_runtime_caltech_rgbt.yaml"
    runtime_config.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    args.log.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "tools/train_mm.py", "--cfg", "configs/_runtime_caltech_rgbt.yaml"]
    environment = dict(os.environ, WORLD_SIZE="1", PYTHONPATH=str(deliver_root), PYTHONUNBUFFERED="1",
                       PYTORCH_CUDA_ALLOC_CONF=os.environ.get("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True"))
    started = time.perf_counter()
    print(json.dumps({"verified": verified, "command": command, "log": str(args.log)}, ensure_ascii=False, indent=2), flush=True)
    with args.log.open("w", encoding="utf-8") as log_stream:
        process = subprocess.run(command, cwd=deliver_root, env=environment, stdout=log_stream, stderr=subprocess.STDOUT)
    summary = {"returncode": process.returncode, "elapsed_seconds": round(time.perf_counter() - started, 1),
               "command": command, "log": str(args.log), "runtime_config": str(runtime_config)}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if process.returncode != 0:
        raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
