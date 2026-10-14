"""Convert simple Labelme polygon JSON to indexed mask PNGs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .images import read_json, read_rgb, save_png


LABELS = {
    "ponding": {"dry": 0, "wet": 1, "ponding": 2, "ignore": 255},
    "shoreline": {"land": 0, "water": 1, "ignore": 255},
    "roi": {"include": 255, "exclude": 0},
}


def labelme_to_mask(annotation: Path, rgb_path: Path, task: str, output: Path) -> dict[str, Any]:
    if task not in LABELS:
        raise ValueError(f"未知标注任务：{task}")
    payload = read_json(annotation)
    rgb = read_rgb(rgb_path)
    height, width = rgb.shape[:2]
    if payload.get("imageHeight") != height or payload.get("imageWidth") != width:
        raise ValueError("Labelme 中的 imageHeight/imageWidth 与 RGB 图像尺寸不一致")
    shapes = payload.get("shapes")
    if not isinstance(shapes, list) or not shapes:
        raise ValueError("Labelme 文件没有 shapes 多边形")
    initial = 0 if task == "roi" else 255
    mask = np.full((height, width), initial, dtype=np.uint8)
    label_counts: dict[str, int] = {}
    for index, shape in enumerate(shapes, start=1):
        if not isinstance(shape, dict) or shape.get("shape_type", "polygon") != "polygon":
            raise ValueError(f"第 {index} 个标注不是 polygon")
        label = shape.get("label")
        if label not in LABELS[task]:
            raise ValueError(f"第 {index} 个标注的类别 {label!r} 不属于 {task}；可用 {list(LABELS[task])}")
        try:
            vertices = np.asarray(shape["points"], dtype=np.float32)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"第 {index} 个标注缺少有效 points") from exc
        if vertices.ndim != 2 or vertices.shape[1] != 2 or len(vertices) < 3 or not np.isfinite(vertices).all():
            raise ValueError(f"第 {index} 个标注需要至少 3 个有限的二维点")
        polygon = np.rint(vertices).astype(np.int32)
        cv2.fillPoly(mask, [polygon], color=LABELS[task][label])
        label_counts[label] = label_counts.get(label, 0) + 1
    save_png(output, mask)
    return {
        "task": task,
        "output": str(output),
        "image_size": {"width": width, "height": height},
        "polygons_by_label": label_counts,
        "pixel_counts": {str(int(value)): int(count) for value, count in zip(*np.unique(mask, return_counts=True))},
    }
