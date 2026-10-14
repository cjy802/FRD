"""Exploratory Caltech paired RGB/thermal water segmentation baseline.

Reads the official ZIP and official paired split text files directly. The three
models use identical image IDs, sampled training pixels, and evaluation pixels.
This is a RandomForest baseline, not a reproduction of the CART paper network.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, replace
from itertools import combinations
from pathlib import Path, PurePosixPath

import cv2
import joblib
import numpy as np
import sklearn
from PIL import Image
from sklearn.ensemble import RandomForestClassifier

from waterwatch.features import feature_stack
from waterwatch.images import Scene, resize_scene, save_json, save_png
from waterwatch.learning import FEATURE_VERSION, metrics_from_confusion, rgb_overlay


DATASET_URL = "https://data.caltech.edu/records/cks6g-ps927"
SPLIT_URL = "https://github.com/aerorobotics/caltech-aerial-rgbt-dataset/tree/main/splits/rgbt_splits"
EXPECTED_ARCHIVE_MD5 = "22f42923694e724d4b7e354bace12389"
MODES = ("rgb", "thermal", "fusion")
SOURCE_LABELS = set(range(12))


@dataclass(frozen=True)
class Pair:
    key: str
    split: str
    rgb: str
    thermal: str
    mask: str

    @property
    def trajectory(self) -> str:
        return PurePosixPath(self.mask).stem.rsplit("_mask-", 1)[0]


def hash_file(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_split_file(path: Path, modality: str) -> dict[str, tuple[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"Official split file missing: {path}")
    result: dict[str, tuple[str, str]] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw.strip():
            continue
        fields = [part.strip().replace("\\", "/") for part in raw.split(",")]
        if len(fields) != 2:
            raise ValueError(f"Expected image,mask in {path}:{line_no}")
        image, mask = fields
        if not image.startswith(f"{modality}/") or not mask.startswith("annotations/"):
            raise ValueError(f"Unexpected paths in {path}:{line_no}: {raw}")
        if mask in result:
            raise ValueError(f"Duplicate mask in {path}: {mask}")
        result[mask] = (image, mask)
    if not result:
        raise ValueError(f"Empty official split: {path}")
    return result


def official_pairs(splits_dir: Path) -> tuple[dict[str, list[Pair]], dict[str, str]]:
    splits: dict[str, list[Pair]] = {}
    hashes: dict[str, str] = {}
    all_keys: set[str] = set()
    for split in ("train", "val", "test"):
        paths = {
            mode: splits_dir / f"{mode}_{split}.txt"
            for mode in ("rgb", "thermal16")
        }
        rgb = parse_split_file(paths["rgb"], "color")
        thermal = parse_split_file(paths["thermal16"], "thermal16")
        if set(rgb) != set(thermal):
            raise ValueError(f"RGB and thermal official {split} splits have different annotation IDs")
        overlap = all_keys & set(rgb)
        if overlap:
            raise ValueError(f"Official splits overlap: {split}, {sorted(overlap)[:3]}")
        all_keys.update(rgb)
        splits[split] = [Pair(key=mask, split=split, rgb=rgb[mask][0], thermal=thermal[mask][0], mask=mask)
                         for mask in sorted(rgb)]
        for path in paths.values():
            hashes[path.name] = hash_file(path)
    return splits, hashes


def zip_member_index(archive: zipfile.ZipFile) -> dict[str, str]:
    """Map official split-relative paths to unique archive entries."""
    members: dict[str, str] = {}
    for name in archive.namelist():
        parts = PurePosixPath(name).parts
        if "__MACOSX" in parts:
            continue
        for position, part in enumerate(parts):
            if part in {"color", "thermal16", "annotations"}:
                relative = "/".join(parts[position:])
                if relative in members and members[relative] != name:
                    raise ValueError(f"Archive has ambiguous member: {relative}")
                members[relative] = name
                break
    return members


def validate_zip_members(pairs: dict[str, list[Pair]], members: dict[str, str]) -> None:
    missing = sorted({path for split in pairs.values() for pair in split
                      for path in (pair.rgb, pair.thermal, pair.mask) if path not in members})
    if missing:
        raise ValueError(f"Official split references missing ZIP members ({len(missing)}): {missing[:5]}")


def decode_mask(raw: bytes) -> np.ndarray:
    """Preserve indexed PNG labels; source 0/1 are ignored, 9 is water."""
    with Image.open(io.BytesIO(raw)) as image:
        mask = np.asarray(image)
    if mask.ndim != 2 or not np.issubdtype(mask.dtype, np.integer):
        raise ValueError("Caltech mask must be a single-channel indexed/integer image")
    source = set(int(label) for label in np.unique(mask))
    if not source <= SOURCE_LABELS:
        raise ValueError(f"Unexpected Caltech labels: {sorted(source - SOURCE_LABELS)}")
    mapped = np.zeros(mask.shape, dtype=np.uint8)
    mapped[mask == 9] = 1
    mapped[(mask == 0) | (mask == 1)] = 255
    return mapped


def _decode_cv(raw: bytes, flags: int, name: str) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), flags)
    if image is None:
        raise ValueError(f"Failed to decode image: {name}")
    return image


def load_pair(archive: zipfile.ZipFile, members: dict[str, str], pair: Pair, max_edge: int) -> Scene:
    rgb = _decode_cv(archive.read(members[pair.rgb]), cv2.IMREAD_COLOR, pair.rgb)
    thermal = _decode_cv(archive.read(members[pair.thermal]), cv2.IMREAD_UNCHANGED, pair.thermal)
    mask = decode_mask(archive.read(members[pair.mask]))
    if thermal.ndim != 2 or thermal.dtype != np.uint16:
        raise ValueError(f"Expected 16-bit one-channel thermal TIFF: {pair.thermal}, got {thermal.shape}/{thermal.dtype}")
    if rgb.shape[:2] != thermal.shape or thermal.shape != mask.shape:
        raise ValueError(f"Unaligned pair {pair.key}: RGB {rgb.shape[:2]}, thermal {thermal.shape}, mask {mask.shape}")
    valid = thermal > 0  # Zero is empty padding after stereo projection.
    if not valid.any():
        raise ValueError(f"No valid thermal pixels: {pair.thermal}")
    scene = Scene(rgb=cv2.cvtColor(rgb, cv2.COLOR_BGR2RGB), thermal=thermal.astype(np.float32),
                  thermal_valid=valid, mask=mask)
    return resize_scene(scene, max_edge)


def select_pairs(pairs: list[Pair], count: int, seed: int) -> list[Pair]:
    if count < 0:
        raise ValueError("Image count must be zero (all) or positive")
    if count == 0 or count >= len(pairs):
        return pairs
    rng = np.random.default_rng(seed)
    indices = sorted(int(index) for index in rng.choice(len(pairs), count, replace=False))
    return [pairs[index] for index in indices]


def _closest_trajectory_groups(counts: Counter[str], target_images: int,
                               preferred_groups: int, reserve_groups: int,
                               rng: np.random.Generator) -> set[str]:
    """Find a small group subset close to an image-count target.

    Exhaustively checks combinations of at most five trajectories. Ties follow
    a seeded permutation, so the assignment is deterministic for a given seed.
    """
    names = sorted(counts)
    rng.shuffle(names)
    max_groups = min(5, len(names) - reserve_groups)
    if max_groups < 1:
        raise ValueError("Not enough trajectories for disjoint splits")
    best_score: tuple[int, int] | None = None
    best: tuple[str, ...] | None = None
    for size in range(1, max_groups + 1):
        for candidate in combinations(names, size):
            total = sum(counts[name] for name in candidate)
            score = (abs(total - target_images), abs(size - preferred_groups))
            if best_score is None or score < best_score:
                best_score, best = score, candidate
    assert best is not None
    return set(best)


def trajectory_split(official: dict[str, list[Pair]], seed: int) -> dict[str, list[Pair]]:
    """Reassign every official paired image to a trajectory-disjoint split."""
    all_pairs = sorted((pair for split in ("train", "val", "test") for pair in official[split]),
                       key=lambda pair: pair.key)
    counts = Counter(pair.trajectory for pair in all_pairs)
    if len(counts) < 3:
        raise ValueError("At least three trajectories are needed for train/val/test")
    rng = np.random.default_rng(seed)
    target_test = len(official["test"])
    target_val = len(official["val"])
    group_goal_test = max(1, round(len(counts) * target_test / len(all_pairs)))
    test_groups = _closest_trajectory_groups(counts, target_test, group_goal_test, 2, rng)
    remaining = Counter({name: count for name, count in counts.items() if name not in test_groups})
    group_goal_val = max(1, round(len(counts) * target_val / len(all_pairs)))
    val_groups = _closest_trajectory_groups(remaining, target_val, group_goal_val, 1, rng)
    train_groups = set(counts) - test_groups - val_groups
    group_sets = {"train": train_groups, "val": val_groups, "test": test_groups}
    result = {name: [replace(pair, split=name) for pair in all_pairs if pair.trajectory in groups]
              for name, groups in group_sets.items()}
    if any(not result[name] for name in result):
        raise ValueError("Trajectory split produced an empty train/val/test partition")
    if sum(map(len, result.values())) != len(all_pairs):
        raise ValueError("Trajectory split lost paired images")
    return result


def common_support(scene: Scene) -> np.ndarray:
    assert scene.mask is not None and scene.thermal_valid is not None
    return scene.thermal_valid & (scene.mask != 255)


def fit_models(archive: zipfile.ZipFile, members: dict[str, str], pairs: list[Pair], *,
               max_edge: int, pixels_per_class: int, trees: int, seed: int,
               jobs: int = 2) -> tuple[dict[str, dict], dict]:
    if jobs < 1:
        raise ValueError("jobs must be positive")
    rng = np.random.default_rng(seed)
    features: dict[str, list[np.ndarray]] = {mode: [] for mode in MODES}
    labels: list[np.ndarray] = []
    sample_counts = Counter()
    decoded = 0
    for pair in pairs:
        scene = load_pair(archive, members, pair, max_edge)
        assert scene.mask is not None
        support = common_support(scene)
        flat_mask = scene.mask.ravel()
        indices = []
        for label in (0, 1):
            candidates = np.flatnonzero(support.ravel() & (flat_mask == label))
            if len(candidates) > pixels_per_class:
                candidates = rng.choice(candidates, pixels_per_class, replace=False)
            indices.append(candidates)
            sample_counts[label] += len(candidates)
        indices = np.concatenate(indices)
        if len(indices):
            labels.append(flat_mask[indices])
            for mode in MODES:
                stack, mode_valid = feature_stack(scene, mode)
                if mode != "rgb" and not np.array_equal(mode_valid, scene.thermal_valid):
                    raise ValueError(f"Thermal support changed unexpectedly for {pair.key}")
                features[mode].append(stack.reshape(-1, stack.shape[2])[indices])
        decoded += 1
        if decoded % 50 == 0:
            print(f"Loaded {decoded}/{len(pairs)} paired training images", flush=True)
    if not sample_counts[0] or not sample_counts[1]:
        raise ValueError(f"Training samples must include water and non-water: {dict(sample_counts)}")
    y = np.concatenate(labels)
    models: dict[str, dict] = {}
    for mode in MODES:
        x = np.concatenate(features[mode])
        classifier = RandomForestClassifier(
            n_estimators=trees, min_samples_leaf=3,
            class_weight="balanced_subsample", random_state=seed, n_jobs=jobs,
        )
        classifier.fit(x, y)
        models[mode] = {
            "task": "shoreline", "mode": mode, "feature_version": FEATURE_VERSION,
            "max_edge": max_edge, "class_names": {0: "non_water", 1: "visible_water"},
            "train_case_ids": [pair.key for pair in pairs], "paired_support": True,
            "classifier": classifier,
        }
        print(f"Fitted {mode} on {len(y)} common pixels", flush=True)
    return models, {"class_counts": {str(label): int(sample_counts[label]) for label in (0, 1)},
                    "total_sampled_pixels": len(y)}


def predict_mask(bundle: dict, scene: Scene, support: np.ndarray, chunk_size: int = 100_000) -> np.ndarray:
    stack, valid = feature_stack(scene, bundle["mode"])
    if bundle["mode"] != "rgb" and not np.array_equal(valid, scene.thermal_valid):
        raise ValueError("Thermal valid support changed unexpectedly")
    flat = stack.reshape(-1, stack.shape[2])
    prediction = np.full(flat.shape[0], 255, dtype=np.uint8)
    indices = np.flatnonzero(support.ravel())
    for offset in range(0, len(indices), chunk_size):
        part = indices[offset:offset + chunk_size]
        prediction[part] = bundle["classifier"].predict(flat[part]).astype(np.uint8)
    return prediction.reshape(support.shape)


def _thermal_preview(scene: Scene) -> np.ndarray:
    assert scene.thermal is not None and scene.thermal_valid is not None
    values = scene.thermal[scene.thermal_valid]
    low, high = np.percentile(values, (1, 99))
    display = np.clip((scene.thermal - low) / max(float(high - low), 1), 0, 1)
    display[~scene.thermal_valid] = 0
    return np.uint8(np.round(display * 255))


def score_split(archive: zipfile.ZipFile, members: dict[str, str], pairs: list[Pair],
                models: dict[str, dict], max_edge: int, examples_dir: Path | None = None) -> dict:
    confusion = {mode: np.zeros((2, 2), np.int64) for mode in MODES}
    cases = []
    saved = 0
    for ordinal, pair in enumerate(pairs, 1):
        scene = load_pair(archive, members, pair, max_edge)
        assert scene.mask is not None and scene.thermal_valid is not None
        support = common_support(scene)
        truth = scene.mask[support]
        predictions = {}
        metrics = {}
        for mode in MODES:
            prediction = predict_mask(models[mode], scene, support)
            predictions[mode] = prediction
            found = prediction[support]
            counts = np.bincount(truth.astype(np.int64) * 2 + found.astype(np.int64), minlength=4).reshape(2, 2)
            confusion[mode] += counts
            tp = int(counts[1, 1])
            union = tp + int(counts[0, 1]) + int(counts[1, 0])
            metrics[mode] = tp / union if union else None
        cases.append({"id": pair.key, "trajectory": pair.trajectory,
                      "image_pixels": int(scene.mask.size), "thermal_nonzero_pixels": int(scene.thermal_valid.sum()),
                      "valid_pixels": int(support.sum()), "water_pixels": int((truth == 1).sum()),
                      "water_iou": metrics})
        if examples_dir is not None and saved < 3 and (truth == 1).any():
            path = examples_dir / PurePosixPath(pair.mask).stem
            save_png(path / "input_rgb.png", cv2.cvtColor(scene.rgb, cv2.COLOR_RGB2BGR))
            save_png(path / "input_thermal.png", _thermal_preview(scene))
            ground_truth = scene.mask.copy()
            ground_truth[~support] = 255
            save_png(path / "truth.png", ground_truth)
            truth_preview = np.zeros(ground_truth.shape, dtype=np.uint8)
            truth_preview[ground_truth == 1] = 255
            truth_preview[ground_truth == 255] = 127
            save_png(path / "truth_preview.png", truth_preview)
            for mode, prediction in predictions.items():
                save_png(path / f"{mode}.png", prediction)
                overlay = rgb_overlay(scene.rgb, prediction, "shoreline")
                save_png(path / f"{mode}_overlay.png", cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))
            saved += 1
        if ordinal % 25 == 0:
            print(f"Scored {ordinal}/{len(pairs)} paired images", flush=True)
    if not cases or not sum(case["valid_pixels"] for case in cases):
        raise ValueError("Evaluation split has no common labeled RGB/thermal pixels")
    supports = {mode: int(matrix.sum()) for mode, matrix in confusion.items()}
    if len(set(supports.values())) != 1:
        raise ValueError(f"Modes were scored on different pixel supports: {supports}")
    results = {}
    for mode, matrix in confusion.items():
        class_metrics = metrics_from_confusion(matrix, "shoreline")
        water = class_metrics["classes"]["visible_water"]
        precision, recall = water["precision"], water["recall"]
        f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
        results[mode] = {"metrics": class_metrics, "water_f1": f1,
                         "pixel_accuracy": float(np.trace(matrix) / matrix.sum())}
    return {"image_count": len(cases), "common_evaluated_pixels": supports["rgb"],
            "per_mode": results, "cases": cases}


def run(args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    if args.max_edge < 64 or args.pixels_per_class < 1 or args.trees < 1 or args.jobs < 1:
        raise ValueError("max_edge must be >=64; pixels_per_class, trees, and jobs must be positive")
    archive_path = args.archive.resolve()
    splits_dir = args.splits_dir.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"Caltech ZIP missing: {archive_path}")
    archive_md5 = hash_file(archive_path, "md5")
    if archive_md5 != EXPECTED_ARCHIVE_MD5:
        raise ValueError("Caltech archive MD5 does not match official published checksum")
    official_splits, split_hashes = official_pairs(splits_dir)
    splits = (official_splits if args.split_strategy == "official"
              else trajectory_split(official_splits, args.seed))
    selected = {name: select_pairs(pairs, getattr(args, f"{name}_count"), args.seed + offset)
                for offset, (name, pairs) in enumerate(splits.items())}
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        members = zip_member_index(archive)
        validate_zip_members(splits, members)
        models, samples = fit_models(
            archive, members, selected["train"], max_edge=args.max_edge,
            pixels_per_class=args.pixels_per_class, trees=args.trees, seed=args.seed, jobs=args.jobs,
        )
        for mode, bundle in models.items():
            joblib.dump(bundle, output / f"caltech_shoreline_{mode}.joblib")
        validation = score_split(archive, members, selected["val"], models, args.max_edge)
        test = score_split(archive, members, selected["test"], models, args.max_edge,
                           examples_dir=output / "examples")
    trajectories = {name: {pair.trajectory for pair in pairs} for name, pairs in splits.items()}
    selected_trajectories = {name: sorted({pair.trajectory for pair in pairs})
                             for name, pairs in selected.items()}
    report = {
        "dataset": {"page": DATASET_URL, "archive": str(archive_path),
                    "archive_bytes": archive_path.stat().st_size,
                    "archive_md5": archive_md5,
                    "expected_archive_md5": EXPECTED_ARCHIVE_MD5,
                    "official_split_source": SPLIT_URL,
                    "official_split_sha256": split_hashes,
                    "official_split_counts": {name: len(pairs) for name, pairs in official_splits.items()},
                    "label_rule": "source 9=water->1; source 0/1=ignore; other 2..11=non-water->0",
                    "thermal_rule": "16-bit relative signal, zero padding excluded; not calibrated temperature"},
        "experiment": {"description": "Paired RGB/thermal/fusion handcrafted pixel features + RandomForest baseline; not CART paper network",
                       "split_strategy": args.split_strategy,
                       "assigned_counts": {name: len(pairs) for name, pairs in splits.items()},
                       "assigned_trajectories": {name: sorted(groups) for name, groups in trajectories.items()},
                       "selected_ids": {name: [pair.key for pair in pairs] for name, pairs in selected.items()},
                       "selected_counts": {name: len(pairs) for name, pairs in selected.items()},
                       "selected_trajectories": selected_trajectories,
                       "software_versions": {"python": platform.python_version(), "numpy": np.__version__,
                                             "opencv": cv2.__version__, "scikit_learn": sklearn.__version__},
                       "seed": args.seed, "max_edge": args.max_edge,
                       "pixels_per_class_per_image": args.pixels_per_class, "trees": args.trees,
                       "random_forest_jobs": args.jobs,
                       "train_sample_counts": samples,
                       "train_test_trajectory_overlap": sorted(trajectories["train"] & trajectories["test"]),
                       "caveats": ["Official RGB-T split is random and can share trajectories across splits; use --split-strategy trajectory for a trajectory-disjoint experiment.",
                                   "The paired set mixes aerial and some ground-level images; these scores are not drone-only.",
                                   "Pixel shape equality does not prove perfect cross-sensor registration.",
                                   "Dataset water includes rivers/lakes and does not label rain puddles or wet road separately."]},
        "validation": validation, "test": test,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }
    save_json(output / "report.json", report)
    summary = {mode: test["per_mode"][mode]["metrics"]["classes"]["visible_water"]["iou"] for mode in MODES}
    print(json.dumps({"test_water_iou": summary, "report": str(output / "report.json")}, indent=2), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/external/labeled_rgbt_pairs.zip"))
    parser.add_argument("--splits-dir", type=Path, default=Path("data/external/caltech_splits"))
    parser.add_argument("--output", type=Path, default=Path("output/reproduction_caltech_rgbt"))
    parser.add_argument("--split-strategy", choices=("official", "trajectory"), default="official")
    parser.add_argument("--train-count", type=int, default=240, help="0 uses all official training images")
    parser.add_argument("--val-count", type=int, default=80, help="0 uses all official validation images")
    parser.add_argument("--test-count", type=int, default=100, help="0 uses all official test images")
    parser.add_argument("--max-edge", type=int, default=256)
    parser.add_argument("--pixels-per-class", type=int, default=80)
    parser.add_argument("--trees", type=int, default=32)
    parser.add_argument("--jobs", type=int, default=2, help="Parallel RandomForest workers; must be positive")
    parser.add_argument("--seed", type=int, default=42)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
