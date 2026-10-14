"""Three comparable RGB / thermal / fusion baselines, trained per task."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier

from .config import CLASSES, IGNORE_LABEL, MODES, TARGET_CLASS
from .features import feature_stack
from .images import Scene, load_scene, resize_scene, save_json, save_png, source_record
from .manifest import Case


FEATURE_VERSION = 1


def _case_sources(case: Case, *, include_thermal: bool) -> dict[str, Any]:
    return {
        "rgb": source_record(case.rgb),
        "mask": source_record(case.mask),
        "thermal": source_record(case.thermal) if include_thermal else None,
        "thermal_registration": source_record(case.thermal_registration) if include_thermal and isinstance(case.thermal_registration, Path) else None,
    }


def _scene_for_case(case: Case, mode: str, max_edge: int, *, paired_support: bool = False) -> Scene:
    needs_thermal = mode != "rgb" or paired_support
    scene = load_scene(
        case.rgb,
        thermal_path=case.thermal if needs_thermal else None,
        thermal_registration=case.thermal_registration if needs_thermal else None,
        mask_path=case.mask,
    )
    return resize_scene(scene, max_edge)


def _checked_labels(mask: np.ndarray, task: str) -> None:
    actual = set(int(value) for value in np.unique(mask))
    allowed = set(CLASSES[task]) | {IGNORE_LABEL}
    unknown = actual - allowed
    if unknown:
        raise ValueError(f"{task} 标注含非法像素值：{sorted(unknown)}；允许值为 {sorted(allowed)}")


def train_model(cases: list[Case], task: str, mode: str, output: Path, *, max_edge: int = 1280, max_per_class: int = 3000, seed: int = 42, paired_support: bool = False) -> dict[str, Any]:
    if task not in CLASSES or mode not in MODES:
        raise ValueError(f"未知任务或模式：{task}/{mode}")
    if max_per_class < 1:
        raise ValueError("max_per_class 必须大于 0")
    if paired_support and mode != "rgb":
        raise ValueError("--paired-support 只适用于 RGB 基线；热像与融合模式天然只用热图重叠区")
    selected = [case for case in cases if case.task == task and case.split == "train"]
    if not selected:
        raise ValueError(f"manifest 中没有 {task} 的 train 案例")
    rng = np.random.default_rng(seed)
    features: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    counts: Counter[int] = Counter()
    for case in selected:
        scene = _scene_for_case(case, mode, max_edge, paired_support=paired_support)
        assert scene.mask is not None
        _checked_labels(scene.mask, task)
        stack, thermal_valid = feature_stack(scene, mode)
        flat_features = stack.reshape(-1, stack.shape[2])
        flat_labels = scene.mask.ravel()
        valid = thermal_valid.ravel() & (flat_labels != IGNORE_LABEL)
        if paired_support:
            assert scene.thermal_valid is not None
            valid &= scene.thermal_valid.ravel()
        for label in CLASSES[task]:
            indices = np.flatnonzero(valid & (flat_labels == label))
            if len(indices) == 0:
                continue
            if len(indices) > max_per_class:
                indices = rng.choice(indices, size=max_per_class, replace=False)
            features.append(flat_features[indices])
            labels.append(flat_labels[indices].astype(np.uint8))
            counts[label] += len(indices)
    missing = set(CLASSES[task]) - set(counts)
    if missing:
        raise ValueError(f"训练样本缺少类别 {sorted(missing)}；需为各类别采集并标注真实像素")
    x_train = np.concatenate(features, axis=0)
    y_train = np.concatenate(labels, axis=0)
    classifier = RandomForestClassifier(
        n_estimators=80,
        min_samples_leaf=3,
        class_weight="balanced_subsample",
        random_state=seed,
        n_jobs=-1,
    )
    classifier.fit(x_train, y_train)
    bundle = {
        "task": task,
        "mode": mode,
        "feature_version": FEATURE_VERSION,
        "max_edge": max_edge,
        "class_names": CLASSES[task],
        "train_case_ids": [case.case_id for case in selected],
        "train_case_sources": {case.case_id: _case_sources(case, include_thermal=mode != "rgb" or paired_support) for case in selected},
        "train_site_ids": sorted({case.site_id for case in selected}),
        "train_site_days": sorted({f"{case.site_id}|{case.captured_at[:10]}" for case in selected}),
        "paired_support": paired_support or mode != "rgb",
        "sample_counts": dict(counts),
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "classifier": classifier,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, output)
    return {key: value for key, value in bundle.items() if key != "classifier"}


def load_model(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到模型：{path}")
    # joblib files are Python pickles: load only models created in this workspace.
    bundle = joblib.load(path)
    if not isinstance(bundle, dict) or bundle.get("feature_version") != FEATURE_VERSION:
        raise ValueError("模型文件格式或特征版本不兼容")
    if bundle.get("task") not in CLASSES or bundle.get("mode") not in MODES or "classifier" not in bundle:
        raise ValueError("模型文件缺少任务、模式或分类器")
    return bundle


def _predict_working(bundle: dict[str, Any], scene: Scene, *, chunk_size: int = 100_000) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    stack, valid = feature_stack(scene, bundle["mode"])
    classifier = bundle["classifier"]
    target = TARGET_CLASS[bundle["task"]]
    classes = [int(value) for value in classifier.classes_]
    if target not in classes:
        raise ValueError(f"模型没有目标类别 {target}")
    target_column = classes.index(target)
    flattened = stack.reshape(-1, stack.shape[2])
    mask = np.full(flattened.shape[0], IGNORE_LABEL, dtype=np.uint8)
    confidence = np.zeros(flattened.shape[0], dtype=np.float32)
    indices = np.flatnonzero(valid.ravel())
    for offset in range(0, len(indices), chunk_size):
        portion = indices[offset:offset + chunk_size]
        mask[portion] = classifier.predict(flattened[portion]).astype(np.uint8)
        confidence[portion] = classifier.predict_proba(flattened[portion])[:, target_column].astype(np.float32)
    height, width = scene.rgb.shape[:2]
    return mask.reshape(height, width), confidence.reshape(height, width), valid


def predict_scene(bundle: dict[str, Any], scene: Scene) -> tuple[np.ndarray, np.ndarray, np.ndarray, tuple[int, int]]:
    working = resize_scene(scene, int(bundle["max_edge"]))
    predicted, confidence, valid = _predict_working(bundle, working)
    processed_shape = predicted.shape
    original_shape = scene.rgb.shape[:2]
    if processed_shape != original_shape:
        width_height = (original_shape[1], original_shape[0])
        predicted = cv2.resize(predicted, width_height, interpolation=cv2.INTER_NEAREST)
        confidence = cv2.resize(confidence, width_height, interpolation=cv2.INTER_NEAREST)
        valid = cv2.resize(valid.astype(np.uint8), width_height, interpolation=cv2.INTER_NEAREST) > 0
    return predicted, confidence, valid, processed_shape


def metrics_from_confusion(confusion: np.ndarray, task: str) -> dict[str, Any]:
    per_class: dict[str, dict[str, float | int | None]] = {}
    for label, name in CLASSES[task].items():
        true_positive = int(confusion[label, label])
        false_positive = int(confusion[:, label].sum() - true_positive)
        false_negative = int(confusion[label, :].sum() - true_positive)
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else None
        recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else None
        union = true_positive + false_positive + false_negative
        iou = true_positive / union if union else None
        per_class[name] = {"support_pixels": int(confusion[label, :].sum()), "precision": precision, "recall": recall, "iou": iou}
    result: dict[str, Any] = {"classes": per_class, "confusion_rows_true_columns_predicted": confusion.tolist()}
    if task == "ponding":
        wet_pixels = int(confusion[1, :].sum())
        result["wet_road_false_ponding_rate"] = float(confusion[1, 2] / wet_pixels) if wet_pixels else None
    return result


def evaluate_model(cases: list[Case], bundle: dict[str, Any], *, split: str = "test") -> dict[str, Any]:
    if split not in {"val", "test"}:
        raise ValueError("评估只接受 val 或 test，不能在训练集上报告测试分数")
    task = bundle["task"]
    selected = [case for case in cases if case.task == task and case.split == split]
    if not selected:
        raise ValueError(f"manifest 没有 {task}/{split} 案例")
    labels = list(CLASSES[task])
    confusion = np.zeros((len(labels), len(labels)), dtype=np.int64)
    case_results = []
    for case in selected:
        scene = _scene_for_case(case, bundle["mode"], int(bundle["max_edge"]), paired_support=bool(bundle.get("paired_support")) and bundle["mode"] == "rgb")
        assert scene.mask is not None
        _checked_labels(scene.mask, task)
        prediction, _, valid = _predict_working(bundle, scene)
        if bundle.get("paired_support") and bundle["mode"] == "rgb":
            assert scene.thermal_valid is not None
            valid &= scene.thermal_valid
        include = valid & (scene.mask != IGNORE_LABEL)
        truth = scene.mask[include].astype(np.int64)
        found = prediction[include].astype(np.int64)
        if len(truth):
            bincount = np.bincount(truth * len(labels) + found, minlength=len(labels) ** 2)
            confusion += bincount.reshape(len(labels), len(labels))
        case_results.append({
            "case_id": case.case_id,
            "site_id": case.site_id,
            "captured_at": case.captured_at,
            "input_sources": _case_sources(case, include_thermal=bool(bundle.get("paired_support")) or bundle["mode"] != "rgb"),
            "evaluated_pixels": int(len(truth)),
            "processed_height": int(prediction.shape[0]),
            "processed_width": int(prediction.shape[1]),
        })
    if not confusion.sum():
        raise ValueError("测试集中没有可评估像素；检查标注和热图覆盖范围")
    train_site_days = set(bundle.get("train_site_days", []))
    same_day_leakage = sorted({case.case_id for case in selected if f"{case.site_id}|{case.captured_at[:10]}" in train_site_days})
    if same_day_leakage:
        raise ValueError(f"训练与 {split} 存在同一地点同一天拍摄的案例，评估可能泄漏：{same_day_leakage}")
    overlapping_sites = sorted(set(bundle.get("train_site_ids", [])) & {case.site_id for case in selected})
    return {
        "task": task,
        "mode": bundle["mode"],
        "split": split,
        "cases": case_results,
        "total_evaluated_pixels": int(confusion.sum()),
        "train_case_ids": bundle.get("train_case_ids", []),
        "paired_thermal_support": bool(bundle.get("paired_support")),
        "overlapping_train_and_evaluation_sites": overlapping_sites,
        "metrics": metrics_from_confusion(confusion, task),
        "note": "仅对当前留出场景有效。比较 RGB/热像/融合时，三者必须使用相同 case_id、相同热图重叠像素，并检查地点重叠。",
    }


def rgb_overlay(rgb: np.ndarray, mask: np.ndarray, task: str) -> np.ndarray:
    if rgb.shape[:2] != mask.shape:
        raise ValueError("叠图要求 RGB 与掩膜等宽高")
    color = rgb.copy()
    palette = {"ponding": {1: (255, 195, 25), 2: (30, 110, 255)}, "shoreline": {1: (30, 110, 255)}}
    for label, shade in palette[task].items():
        color[mask == label] = shade
    mixed = cv2.addWeighted(rgb, 0.6, color, 0.4, 0)
    mixed[mask == IGNORE_LABEL] = rgb[mask == IGNORE_LABEL]
    return mixed


def save_prediction(output: Path, bundle: dict[str, Any], scene: Scene, metadata: dict[str, Any], roi: np.ndarray, event: dict[str, Any] | None = None, sources: dict[str, Any] | None = None) -> dict[str, Any]:
    if roi.shape != scene.rgb.shape[:2] or roi.ndim != 2:
        raise ValueError("ROI 必须是与 RGB 等宽高的单通道图")
    mask, confidence, valid, processed_shape = predict_scene(bundle, scene)
    valid &= roi != 0
    if not valid.any():
        raise ValueError("ROI 与模型有效影像区域没有重叠像素")
    mask[~valid] = IGNORE_LABEL
    confidence[~valid] = 0
    output.mkdir(parents=True, exist_ok=True)
    save_png(output / "classes.png", mask)
    target = TARGET_CLASS[bundle["task"]]
    save_png(output / "suspected.png", ((mask == target) * 255).astype(np.uint8))
    save_png(output / "confidence.png", np.clip(confidence * 255, 0, 255).astype(np.uint8))
    save_png(output / "overlay.png", cv2.cvtColor(rgb_overlay(scene.rgb, mask, bundle["task"]), cv2.COLOR_RGB2BGR))
    report = {
        "task": bundle["task"],
        "mode": bundle["mode"],
        "metadata": metadata,
        "event_context": event,
        "input_sources": sources,
        "image_size": {"width": int(mask.shape[1]), "height": int(mask.shape[0])},
        "model_processed_size": {"width": int(processed_shape[1]), "height": int(processed_shape[0])},
        "thermal_overlap_fraction": float(np.mean(valid)) if bundle["mode"] != "rgb" else None,
        "thermal_registration": scene.registration_quality,
        "suspected_pixels": int((mask == target).sum()),
        "valid_pixels": int(valid.sum()),
        "roi_excluded_pixels": int((roi == 0).sum()),
        "pixel_area_warning": "像素数不是平方米；未做地面尺度或正射测绘标定。",
        "interpretation": "这是用户样本训练的试验性疑点筛查，不是实测积水深度、水位或堤防安全结论。",
    }
    save_json(output / "report.json", report)
    return report
