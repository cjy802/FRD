"""RGB box-crop classification baseline for the Mendeley water/wet-surface data.

Each supplied YOLO box is cropped using its ground-truth coordinates and classified
as water or wet surface. This is *not* object detection or pixel segmentation: at
inference time a detector would first need to propose the boxes. No thermal data
is available in this dataset.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import cv2
import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import confusion_matrix, f1_score, precision_recall_fscore_support
from sklearn.model_selection import train_test_split


DATASET_PAGE = "https://data.mendeley.com/datasets/y6zyrnxbfm/4"
DATASET_DOI = "10.17632/y6zyrnxbfm.4"
PAPER_DOI = "10.1016/j.dib.2021.107752"
SMALL_ARCHIVE_SHA256 = "84e08c2c2dea792b7b06b431fb898a0392127873b67b5a5d940654c6a84ba117"
CLASS_NAMES = ("water", "wet_surface")


@dataclass(frozen=True)
class Box:
    label: int
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class ImageRecord:
    path: str
    digest: str
    boxes: tuple[Box, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_yolo_labels(raw: bytes, name: str) -> tuple[list[Box], int]:
    """Return valid YOLO center boxes and the count of zero-area annotations."""
    boxes: list[Box] = []
    zero_area = 0
    for line_number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 5:
            raise ValueError(f"{name}:{line_number}: expected class cx cy width height")
        try:
            label = int(parts[0])
            x, y, width, height = map(float, parts[1:])
        except ValueError as error:
            raise ValueError(f"{name}:{line_number}: invalid numeric annotation") from error
        if label not in (0, 1):
            raise ValueError(f"{name}:{line_number}: unexpected class {label}")
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (x, y, width, height)):
            raise ValueError(f"{name}:{line_number}: coordinates must be finite and within [0, 1]")
        if (x - width / 2 < -1e-5 or x + width / 2 > 1 + 1e-5
                or y - height / 2 < -1e-5 or y + height / 2 > 1 + 1e-5):
            raise ValueError(f"{name}:{line_number}: box extends beyond the image")
        if width == 0 or height == 0:
            zero_area += 1
            continue
        boxes.append(Box(label, x, y, width, height))
    return boxes, zero_area


def archive_image_paths(archive: zipfile.ZipFile) -> list[str]:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("ZIP contains duplicate member names")
    roots = [name.removesuffix("classes.txt") for name in names if name.endswith("classes.txt")]
    if len(roots) != 1:
        raise ValueError("Expected one classes.txt at the data root")
    root = roots[0]
    classes = archive.read(root + "classes.txt").decode("utf-8-sig").splitlines()
    if [value.strip().lower() for value in classes if value.strip()] != ["water", "wet surface"]:
        raise ValueError("Expected source class IDs 0=water, 1=wet surface")
    images = sorted(name for name in names if name.startswith(root) and name.lower().endswith(".jpeg"))
    if not images or any(name.removesuffix(".jpeg") + ".txt" not in names for name in images):
        raise ValueError("Images and annotation files are not paired")
    return images


def inspect_archive(archive: zipfile.ZipFile) -> tuple[list[ImageRecord], dict]:
    """Check all labels, then keep one deterministic image from each SHA group."""
    image_paths = archive_image_paths(archive)
    by_digest: dict[str, list[ImageRecord]] = defaultdict(list)
    source_counts: Counter[int] = Counter()
    zero_area = 0
    empty_labels = 0
    for path in image_paths:
        raw_image = archive.read(path)  # ZIP read checks this member's CRC.
        decoded = cv2.imdecode(np.frombuffer(raw_image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if decoded is None or decoded.shape[:2] != (256, 256):
            raise ValueError(f"Expected decodable 256x256 JPEG: {path}")
        label_path = path.removesuffix(".jpeg") + ".txt"
        boxes, dropped = parse_yolo_labels(archive.read(label_path), label_path)
        zero_area += dropped
        empty_labels += not boxes
        source_counts.update(box.label for box in boxes)
        digest = hashlib.sha256(raw_image).hexdigest()
        by_digest[digest].append(ImageRecord(path, digest, tuple(boxes)))

    canonical: list[ImageRecord] = []
    conflicting_groups = 0
    class_presence_conflicts = 0
    duplicate_groups = 0
    for records in by_digest.values():
        records.sort(key=lambda item: item.path)
        canonical.append(records[0])
        if len(records) > 1:
            duplicate_groups += 1
            conflicting_groups += len({record.boxes for record in records}) > 1
            class_presence_conflicts += len({frozenset(box.label for box in record.boxes) for record in records}) > 1
    canonical.sort(key=lambda item: item.path)
    usable = [record for record in canonical if record.boxes]
    stats = {
        "source_images": len(image_paths),
        "source_valid_boxes_by_class": {CLASS_NAMES[k]: source_counts[k] for k in (0, 1)},
        "source_zero_area_boxes_dropped": zero_area,
        "source_images_without_valid_boxes": empty_labels,
        "exact_duplicate_sha256_groups": duplicate_groups,
        "duplicate_extra_images_removed": len(image_paths) - len(canonical),
        "duplicate_groups_with_conflicting_box_labels": conflicting_groups,
        "duplicate_groups_with_conflicting_class_presence": class_presence_conflicts,
        "canonical_images": len(canonical),
        "canonical_images_with_valid_boxes": len(usable),
        "canonical_boxes_by_class": {
            CLASS_NAMES[k]: sum(box.label == k for record in usable for box in record.boxes)
            for k in (0, 1)
        },
    }
    return usable, stats


def split_images(records: list[ImageRecord], seed: int) -> dict[str, list[ImageRecord]]:
    """Stratify by image-level class presence; boxes from an image never split."""
    if len(records) < 20:
        raise ValueError("Need at least 20 labeled, deduplicated images")
    strata = [",".join(str(label) for label in sorted({box.label for box in record.boxes})) for record in records]
    train, remainder = train_test_split(
        records, test_size=0.30, random_state=seed, stratify=strata,
    )
    remaining_strata = [",".join(str(label) for label in sorted({box.label for box in record.boxes})) for record in remainder]
    validation, test = train_test_split(
        remainder, test_size=0.50, random_state=seed, stratify=remaining_strata,
    )
    result = {"train": sorted(train, key=lambda item: item.path),
              "validation": sorted(validation, key=lambda item: item.path),
              "test": sorted(test, key=lambda item: item.path)}
    digests = [{record.digest for record in values} for values in result.values()]
    if any(digests[i] & digests[j] for i in range(3) for j in range(i + 1, 3)):
        raise AssertionError("Exact duplicate image SHA crossed split boundaries")
    return result


def box_pixels(image: np.ndarray, box: Box) -> np.ndarray:
    height, width = image.shape[:2]
    left = max(0, math.floor((box.x - box.width / 2) * width))
    top = max(0, math.floor((box.y - box.height / 2) * height))
    right = min(width, math.ceil((box.x + box.width / 2) * width))
    bottom = min(height, math.ceil((box.y + box.height / 2) * height))
    if right <= left or bottom <= top:
        raise ValueError("Box became empty in pixel coordinates")
    return image[top:bottom, left:right]


def crop_features(crop: np.ndarray) -> np.ndarray:
    """Simple color, brightness, texture and reflection-cue features."""
    image = cv2.resize(crop, (48, 48), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    parts: list[np.ndarray] = []
    for source_index, source in enumerate((rgb, hsv)):
        for channel_index, channel in enumerate(cv2.split(source)):
            scale = 179.0 if source_index == 1 and channel_index == 0 else 255.0
            values = channel.astype(np.float32) / scale
            parts.append(np.array([values.mean(), values.std()], dtype=np.float32))
            parts.append(np.histogram(values, bins=8, range=(0, 1))[0].astype(np.float32) / values.size)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    edge = cv2.magnitude(gx, gy)
    local_mean = cv2.GaussianBlur(gray, (7, 7), 0)
    texture = np.sqrt(np.maximum(cv2.GaussianBlur(gray * gray, (7, 7), 0) - local_mean**2, 0))
    saturation = hsv[:, :, 1].astype(np.float32) / 255.0
    brightness = hsv[:, :, 2].astype(np.float32) / 255.0
    reflection = brightness * (1.0 - saturation) * np.exp(-10.0 * texture)
    parts.append(np.array([
        edge.mean(), edge.std(), edge.max(),
        texture.mean(), texture.std(), reflection.mean(), reflection.std(),
    ], dtype=np.float32))
    return np.concatenate(parts)


def features_for_split(archive: zipfile.ZipFile, records: list[ImageRecord]) -> tuple[np.ndarray, np.ndarray]:
    features: list[np.ndarray] = []
    labels: list[int] = []
    for record in records:
        raw = archive.read(record.path)
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"Cannot decode {record.path}")
        for box in record.boxes:
            features.append(crop_features(box_pixels(image, box)))
            labels.append(box.label)
    return np.stack(features), np.asarray(labels, dtype=np.uint8)


def score(model: RandomForestClassifier, features: np.ndarray, labels: np.ndarray) -> dict:
    predicted = model.predict(features)
    matrix = confusion_matrix(labels, predicted, labels=[0, 1])
    precision, recall, f1, support = precision_recall_fscore_support(
        labels, predicted, labels=[0, 1], zero_division=0,
    )
    return {
        "box_count": int(labels.size),
        "class_support": {name: int(support[i]) for i, name in enumerate(CLASS_NAMES)},
        "confusion_rows_true_columns_predicted": matrix.tolist(),
        "class_precision": {name: float(precision[i]) for i, name in enumerate(CLASS_NAMES)},
        "class_recall": {name: float(recall[i]) for i, name in enumerate(CLASS_NAMES)},
        "class_f1": {name: float(f1[i]) for i, name in enumerate(CLASS_NAMES)},
        "macro_f1": float(f1_score(labels, predicted, average="macro")),
    }


def run(args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    archive_path = args.archive.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(archive_path)
    archive_digest = sha256_file(archive_path)
    if archive_digest != SMALL_ARCHIVE_SHA256:
        raise ValueError(f"Expected the audited 34.9 MiB source ZIP, got SHA256 {archive_digest}")
    if args.trees < 1:
        raise ValueError("trees must be positive")
    with zipfile.ZipFile(archive_path) as archive:
        records, inspected = inspect_archive(archive)
        splits = split_images(records, args.seed)
        arrays = {key: features_for_split(archive, group) for key, group in splits.items()}
    train_x, train_y = arrays["train"]
    model = RandomForestClassifier(
        n_estimators=args.trees, max_depth=18, min_samples_leaf=4,
        class_weight="balanced_subsample", n_jobs=-1, random_state=args.seed,
    )
    model.fit(train_x, train_y)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, output / "model.joblib")
    report = {
        "dataset": {
            "page": DATASET_PAGE, "dataset_doi": DATASET_DOI, "paper_doi": PAPER_DOI,
            "archive": str(archive_path), "archive_sha256": archive_digest,
            "license": "CC BY 4.0 per Mendeley version-4 landing page",
            "source_version_note": "This small ZIP contains 2000 early-version images; the version-4 page describes 1976 cleaned images in a larger ZIP.",
            "source_class_ids": {"0": "water", "1": "wet surface"},
            "audit": inspected,
        },
        "experiment": {
            "task": "RGB classification of ground-truth YOLO box crops; no box proposal, detection, segmentation, or thermal fusion",
            "features": "RGB/HSV crop summaries and histograms, edge/texture statistics, reflection brightness cue",
            "model": {"name": "RandomForestClassifier", "trees": args.trees, "max_depth": 18,
                      "min_samples_leaf": 4, "class_weight": "balanced_subsample"},
            "seed": args.seed,
            "split_rule": "deduplicate byte-identical image SHA256; retain lexicographically first label set; stratify image-level class presence; 70/15/15 whole-image split",
            "split_images": {key: len(group) for key, group in splits.items()},
            "split_boxes": {key: int(values[1].size) for key, values in arrays.items()},
            "split_image_paths": {key: [record.path for record in group] for key, group in splits.items()},
            "limitations": [
                "The same annotated ground-truth boxes are supplied for crop extraction at evaluation time; no water-location detection is measured.",
                "Source images are phone views at roughly human height, not drone RGB images; there is no thermal channel.",
                "No scene, location, or rainfall-event IDs exist in the small archive; near-duplicate scenes may cross image-level splits.",
                "All exact-duplicate image groups contain conflicting box annotations; one canonical annotation set is chosen deterministically.",
                "Box annotations are coarse and sometimes include adjacent terrain; results do not establish pixel-level water boundaries.",
            ],
        },
        "validation": score(model, *arrays["validation"]),
        "test": score(model, *arrays["test"]),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "source_images": inspected["source_images"],
        "canonical_images": inspected["canonical_images"],
        "split_images": report["experiment"]["split_images"],
        "test_confusion": report["test"]["confusion_rows_true_columns_predicted"],
        "test_macro_f1": report["test"]["macro_f1"],
        "report": str(output / "report.json"),
    }, ensure_ascii=False, indent=2), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/external/stagnant_water_wet_surface_34mb.zip"))
    parser.add_argument("--output", type=Path, default=Path("output/reproduction_wet_water_boxes"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--trees", type=int, default=64)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
