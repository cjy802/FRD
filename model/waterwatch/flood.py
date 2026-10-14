"""River/lake shoreline change from two labeled or predicted water masks."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .images import homography_from_file, read_mask, read_rgb, save_json, save_png, source_record, warp_to


def _check_shoreline_mask(mask: np.ndarray) -> None:
    unexpected = set(int(value) for value in np.unique(mask)) - {0, 1, 255}
    if unexpected:
        raise ValueError(f"岸线掩膜只允许 0=陆地、1=可见水面、255=忽略；发现 {sorted(unexpected)}")


def _remove_small_regions(mask: np.ndarray, minimum: int) -> np.ndarray:
    if minimum <= 1:
        return mask
    count, components, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    result = np.zeros(mask.shape, bool)
    for component in range(1, count):
        if int(stats[component, cv2.CC_STAT_AREA]) >= minimum:
            result[components == component] = True
    return result


def shoreline_change(
    before_rgb_path: Path,
    before_mask_path: Path,
    after_rgb_path: Path,
    after_mask_path: Path,
    before_metadata: dict[str, Any],
    after_metadata: dict[str, Any],
    output: Path,
    *,
    registration: Path | None = None,
    same_view: bool = False,
    event: dict[str, Any] | None = None,
    min_component_pixels: int = 1,
    before_mask_origin: str = "unverified",
    after_mask_origin: str = "unverified",
) -> dict[str, Any]:
    if (registration is None) == (not same_view):
        raise ValueError("岸线变化需且仅需一个选项：--registration 或 --same-view")
    if before_metadata["site_id"] != after_metadata["site_id"]:
        raise ValueError("前后影像必须来自相同 site_id")
    before_time = datetime.fromisoformat(before_metadata["captured_at"])
    after_time = datetime.fromisoformat(after_metadata["captured_at"])
    if after_time <= before_time:
        raise ValueError("after 影像的拍摄时间必须晚于 before")
    if min_component_pixels < 1:
        raise ValueError("min_component_pixels 必须大于 0")
    allowed_origins = {"manual", "model", "unverified"}
    if before_mask_origin not in allowed_origins or after_mask_origin not in allowed_origins:
        raise ValueError("掩膜来源只能是 manual、model 或 unverified")
    before_rgb = read_rgb(before_rgb_path)
    after_rgb = read_rgb(after_rgb_path)
    before_mask = read_mask(before_mask_path, before_rgb.shape[:2])
    after_mask = read_mask(after_mask_path, after_rgb.shape[:2])
    _check_shoreline_mask(before_mask)
    _check_shoreline_mask(after_mask)
    quality: dict[str, float | int] | None = None
    if registration is not None:
        matrix, quality = homography_from_file(registration)
        after_aligned, coverage = warp_to(after_mask, matrix, before_mask.shape, nearest=True)
    else:
        if after_mask.shape != before_mask.shape:
            raise ValueError("--same-view 要求两张图等宽高；若视角变化，提供配准控制点")
        after_aligned = after_mask
        coverage = np.ones(before_mask.shape, bool)
    valid = coverage & (before_mask != 255) & (after_aligned != 255)
    newly_water = _remove_small_regions(valid & (before_mask == 0) & (after_aligned == 1), min_component_pixels)
    receded_water = _remove_small_regions(valid & (before_mask == 1) & (after_aligned == 0), min_component_pixels)
    output.mkdir(parents=True, exist_ok=True)
    save_png(output / "newly_water.png", (newly_water * 255).astype(np.uint8))
    save_png(output / "receded_water.png", (receded_water * 255).astype(np.uint8))
    colored = before_rgb.copy()
    colored[newly_water] = (30, 110, 255)
    colored[receded_water] = (255, 190, 20)
    overlay = cv2.addWeighted(before_rgb, 0.6, colored, 0.4, 0)
    save_png(output / "change_overlay.png", cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))
    report = {
        "task": "shoreline_change",
        "site_id": before_metadata["site_id"],
        "before_metadata": before_metadata,
        "after_metadata": after_metadata,
        "event_context": event,
        "input_sources": {
            "before_rgb": source_record(before_rgb_path),
            "before_mask": source_record(before_mask_path),
            "after_rgb": source_record(after_rgb_path),
            "after_mask": source_record(after_mask_path),
            "registration": source_record(registration),
        },
        "mask_origins": {"before": before_mask_origin, "after": after_mask_origin},
        "registration_quality": quality,
        "alignment_assumption": "explicit_same_view" if same_view else "control_point_homography",
        "covered_fraction": float(np.mean(coverage)),
        "valid_comparison_pixels": int(valid.sum()),
        "newly_water_pixels": int(newly_water.sum()),
        "receded_water_pixels": int(receded_water.sum()),
        "min_component_pixels": min_component_pixels,
        "interpretation": "这里只比较可见水面像素；没有地面尺度或水尺标定，不能转换为平方米、水位、水深或流量。",
    }
    save_json(output / "report.json", report)
    return report
