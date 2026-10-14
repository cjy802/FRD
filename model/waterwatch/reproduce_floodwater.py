"""Reproducible RGB water-segmentation baseline on the Floodwater manual ZIP.

This uses the public annotations, not the paper's Temporal Token Reuse model.
The manual ZIP does not provide source-video IDs or an official train/test split.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import zipfile
from collections import Counter
from pathlib import Path

import cv2
import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier

from .config import CLASSES
from .features import feature_stack
from .images import Scene, resize_scene, save_json, save_png
from .learning import FEATURE_VERSION, metrics_from_confusion, rgb_overlay


PREFIX = "floodwater_manual/"
DATASET_PAGE = "https://github.com/decide-ugent/floodwater-dataset"


def stems_in_archive(archive: zipfile.ZipFile) -> list[str]:
    """Require one JPEG and one PNG for each manual sample."""
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("ZIP contains duplicate member names")
    images = {
        name.removeprefix(PREFIX + "images/").removesuffix(".jpg")
        for name in names
        if name.startswith(PREFIX + "images/") and name.endswith(".jpg")
    }
    masks = {
        name.removeprefix(PREFIX + "masks/").removesuffix(".png")
        for name in names
        if name.startswith(PREFIX + "masks/") and name.endswith(".png")
    }
    if not images or images != masks:
        raise ValueError(f"Unpaired samples: images={len(images)}, masks={len(masks)}")
    if any(not stem.startswith("sample_") for stem in images):
        raise ValueError("Unexpected sample name")
    return sorted(images)


def decode_mask(raw: bytes) -> np.ndarray:
    mask = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if mask is None or mask.ndim != 2 or mask.dtype != np.uint8:
        raise ValueError("Floodwater mask must be an 8-bit single-channel PNG")
    labels = set(np.unique(mask).tolist())
    if not labels <= {0, 255}:
        raise ValueError(f"Unexpected Floodwater mask values: {sorted(labels)}")
    # The source dataset uses 255 for water. This project uses 255 for ignore.
    return (mask == 255).astype(np.uint8)


def load_pair(archive: zipfile.ZipFile, stem: str, max_edge: int) -> Scene:
    image_name = PREFIX + "images/" + stem + ".jpg"
    mask_name = PREFIX + "masks/" + stem + ".png"
    bgr = cv2.imdecode(np.frombuffer(archive.read(image_name), dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError(f"Cannot decode {image_name}")
    mask = decode_mask(archive.read(mask_name))
    if bgr.shape[:2] != mask.shape:
        raise ValueError(f"Image and mask shapes differ for {stem}")
    scene = Scene(rgb=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), mask=mask)
    return resize_scene(scene, max_edge)


def inspect_archive(archive: zipfile.ZipFile, stems: list[str]) -> dict:
    """Read every pair once; ZIP reads also verify per-member CRC."""
    shapes: Counter[str] = Counter()
    pixel_counts: Counter[int] = Counter()
    image_hashes: dict[str, str] = {}
    water_fractions: dict[str, float] = {}
    for number, stem in enumerate(stems, start=1):
        image_raw = archive.read(PREFIX + "images/" + stem + ".jpg")
        mask_raw = archive.read(PREFIX + "masks/" + stem + ".png")
        bgr = cv2.imdecode(np.frombuffer(image_raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError(f"Cannot decode RGB image for {stem}")
        mask = decode_mask(mask_raw)
        if bgr.shape[:2] != mask.shape:
            raise ValueError(f"Image and mask shapes differ for {stem}")
        shapes[f"{bgr.shape[1]}x{bgr.shape[0]}"] += 1
        water = int(mask.sum())
        pixel_counts[1] += water
        pixel_counts[0] += int(mask.size) - water
        water_fractions[stem] = water / mask.size
        image_hashes[stem] = hashlib.sha256(image_raw).hexdigest()
        if number % 100 == 0:
            print(f"Checked {number}/{len(stems)} RGB/mask pairs", flush=True)
    return {
        "pair_count": len(stems),
        "image_sizes": dict(shapes),
        "source_pixel_counts": {"non_water": pixel_counts[0], "water": pixel_counts[1]},
        "water_fraction_min": min(water_fractions.values()),
        "water_fraction_median": float(np.median(list(water_fractions.values()))),
        "water_fraction_max": max(water_fractions.values()),
        "image_hashes": image_hashes,
        "water_fractions": water_fractions,
    }


def sequential_split(stems: list[str], train_count: int, val_count: int) -> dict[str, list[str]]:
    if train_count < 1 or val_count < 1 or train_count + val_count >= len(stems):
        raise ValueError("Need positive, disjoint train, validation and test blocks")
    return {
        "train": stems[:train_count],
        "val": stems[train_count:train_count + val_count],
        "test": stems[train_count + val_count:],
    }


def cross_split_exact_duplicates(splits: dict[str, list[str]], hashes: dict[str, str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for left, right in (("train", "val"), ("train", "test"), ("val", "test")):
        left_hashes = {hashes[stem] for stem in splits[left]}
        result[f"{left}_{right}"] = sum(hashes[stem] in left_hashes for stem in splits[right])
    return result


def fit_rgb_baseline(
    archive: zipfile.ZipFile,
    stems: list[str],
    *,
    max_edge: int,
    samples_per_class: int,
    trees: int,
    seed: int,
) -> tuple[dict, dict[int, int]]:
    if samples_per_class < 1 or trees < 1:
        raise ValueError("samples_per_class and trees must be positive")
    rng = np.random.default_rng(seed)
    features: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    counts: Counter[int] = Counter()
    for number, stem in enumerate(stems, start=1):
        scene = load_pair(archive, stem, max_edge)
        assert scene.mask is not None
        stack, valid = feature_stack(scene, "rgb")
        flat_features = stack.reshape(-1, stack.shape[2])
        flat_labels = scene.mask.ravel()
        for label in (0, 1):
            indices = np.flatnonzero(valid.ravel() & (flat_labels == label))
            if len(indices) > samples_per_class:
                indices = rng.choice(indices, size=samples_per_class, replace=False)
            if len(indices):
                features.append(flat_features[indices])
                labels.append(flat_labels[indices])
                counts[label] += len(indices)
        if number % 100 == 0:
            print(f"Extracted training pixels from {number}/{len(stems)} images", flush=True)
    if not all(counts[label] > 0 for label in (0, 1)):
        raise ValueError("Training data must contain both water and non-water pixels")
    classifier = RandomForestClassifier(
        n_estimators=trees,
        min_samples_leaf=3,
        class_weight="balanced_subsample",
        random_state=seed,
        n_jobs=-1,
    )
    classifier.fit(np.concatenate(features), np.concatenate(labels))
    bundle = {
        "task": "shoreline",
        "mode": "rgb",
        "feature_version": FEATURE_VERSION,
        "max_edge": max_edge,
        "class_names": CLASSES["shoreline"],
        "train_case_ids": stems,
        "sample_counts": dict(counts),
        "paired_support": False,
        "classifier": classifier,
    }
    return bundle, dict(counts)


def predict_mask(bundle: dict, scene: Scene, chunk_size: int = 100_000) -> np.ndarray:
    stack, valid = feature_stack(scene, "rgb")
    flat = stack.reshape(-1, stack.shape[2])
    prediction = np.zeros(flat.shape[0], dtype=np.uint8)
    for start in range(0, len(flat), chunk_size):
        end = min(start + chunk_size, len(flat))
        prediction[start:end] = bundle["classifier"].predict(flat[start:end]).astype(np.uint8)
    return prediction.reshape(valid.shape)


def score_split(
    archive: zipfile.ZipFile,
    stems: list[str],
    bundle: dict,
    output: Path,
    *,
    save_examples: bool,
) -> dict:
    confusion = np.zeros((2, 2), dtype=np.int64)
    cases: list[dict] = []
    selected_examples = {stems[0], stems[len(stems) // 2], stems[-1]} if save_examples else set()
    for number, stem in enumerate(stems, start=1):
        scene = load_pair(archive, stem, int(bundle["max_edge"]))
        assert scene.mask is not None
        predicted = predict_mask(bundle, scene)
        counts = np.bincount((scene.mask.ravel() * 2 + predicted.ravel()), minlength=4).reshape(2, 2)
        confusion += counts
        water_tp = int(counts[1, 1])
        water_union = water_tp + int(counts[0, 1]) + int(counts[1, 0])
        cases.append({
            "sample_id": stem,
            "evaluated_pixels": int(scene.mask.size),
            "water_fraction": float(scene.mask.mean()),
            "water_iou": water_tp / water_union if water_union else None,
        })
        if stem in selected_examples:
            example_dir = output / "examples" / stem
            save_png(example_dir / "rgb.png", cv2.cvtColor(scene.rgb, cv2.COLOR_RGB2BGR))
            save_png(example_dir / "ground_truth.png", scene.mask * 255)
            save_png(example_dir / "prediction.png", predicted * 255)
            overlay = rgb_overlay(scene.rgb, predicted, "shoreline")
            save_png(example_dir / "overlay.png", cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))
        if number % 25 == 0:
            print(f"Scored {number}/{len(stems)} held-out images", flush=True)
    metrics = metrics_from_confusion(confusion, "shoreline")
    water = metrics["classes"]["visible_water"]
    precision, recall = water["precision"], water["recall"]
    water_ious = [case["water_iou"] for case in cases if case["water_iou"] is not None]
    return {
        "image_count": len(stems),
        "evaluated_pixels": int(confusion.sum()),
        "metrics": metrics,
        "water_f1": 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None,
        "mean_class_iou": float(np.mean([item["iou"] for item in metrics["classes"].values() if item["iou"] is not None])),
        "pixel_accuracy": float(np.trace(confusion) / confusion.sum()),
        "median_image_water_iou": float(np.median(water_ious)),
        "min_image_water_iou": float(min(water_ious)),
        "max_image_water_iou": float(max(water_ious)),
        "cases": cases,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    archive_path = args.archive.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"Dataset ZIP missing: {archive_path}")
    if args.max_edge < 64:
        raise ValueError("max_edge must be at least 64")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        stems = stems_in_archive(archive)
        if len(stems) != 700:
            raise ValueError(f"Expected 700 manual pairs, found {len(stems)}")
        splits = sequential_split(stems, args.train_count, args.val_count)
        inspected = inspect_archive(archive, stems)
        duplicate_counts = cross_split_exact_duplicates(splits, inspected["image_hashes"])
        if any(duplicate_counts.values()):
            raise ValueError(f"Byte-identical RGB images cross split boundaries: {duplicate_counts}")
        bundle, sample_counts = fit_rgb_baseline(
            archive,
            splits["train"],
            max_edge=args.max_edge,
            samples_per_class=args.samples_per_class,
            trees=args.trees,
            seed=args.seed,
        )
        model_path = args.model.resolve()
        model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(bundle, model_path)
        validation = score_split(archive, splits["val"], bundle, output, save_examples=False)
        test = score_split(archive, splits["test"], bundle, output, save_examples=True)
    report = {
        "dataset": {
            "page": DATASET_PAGE,
            "archive": str(archive_path),
            "archive_sha256": sha256_file(archive_path),
            "license": "GPL-3.0 per author repository",
            "label_conversion": {"source_0": "non_water -> project 0", "source_255": "water -> project 1"},
            "checked_pairs": inspected["pair_count"],
            "original_image_sizes": inspected["image_sizes"],
            "original_pixel_counts": inspected["source_pixel_counts"],
            "source_water_fraction_range": [inspected["water_fraction_min"], inspected["water_fraction_max"]],
            "source_water_fraction_median": inspected["water_fraction_median"],
        },
        "experiment": {
            "description": "RGB-only pixel-feature RandomForest baseline; not the paper's TTR model",
            "split_rule": "sorted sample filename blocks; source-video IDs unavailable",
            "split_ids": splits,
            "cross_split_byte_identical_image_counts": duplicate_counts,
            "seed": args.seed,
            "max_image_edge_for_training_and_evaluation": args.max_edge,
            "sampled_training_pixels_per_class_per_image": args.samples_per_class,
            "training_sample_counts": sample_counts,
            "random_forest_trees": args.trees,
            "model_path": str(model_path),
            "limitation": "No source-video identifiers, so nearby or visually similar frames may cross splits; this is an exploratory split, not an independent-scene benchmark.",
        },
        "validation": validation,
        "test": test,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }
    save_json(output / "report.json", report)
    summary = {
        "checked_pairs": inspected["pair_count"],
        "train_val_test": {key: len(value) for key, value in splits.items()},
        "water_test_iou": test["metrics"]["classes"]["visible_water"]["iou"],
        "water_test_precision": test["metrics"]["classes"]["visible_water"]["precision"],
        "water_test_recall": test["metrics"]["classes"]["visible_water"]["recall"],
        "report": str(output / "report.json"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/external/floodwater_manual.zip"))
    parser.add_argument("--model", type=Path, default=Path("artifacts/floodwater_shoreline_rgb.joblib"))
    parser.add_argument("--output", type=Path, default=Path("output/reproduction_floodwater"))
    parser.add_argument("--train-count", type=int, default=500)
    parser.add_argument("--val-count", type=int, default=100)
    parser.add_argument("--max-edge", type=int, default=320)
    parser.add_argument("--samples-per-class", type=int, default=120)
    parser.add_argument("--trees", type=int, default=48)
    parser.add_argument("--seed", type=int, default=42)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
