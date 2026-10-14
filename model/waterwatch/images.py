"""Image I/O and explicit planar registration for separately captured sensors."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class Scene:
    rgb: np.ndarray
    thermal: np.ndarray | None = None
    thermal_valid: np.ndarray | None = None
    mask: np.ndarray | None = None
    registration_quality: dict[str, float | int] | None = None


def _imread(path: Path, flags: int) -> np.ndarray:
    if not path.is_file():
        raise FileNotFoundError(f"找不到图像：{path}")
    raw = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(raw, flags)
    if image is None:
        raise ValueError(f"无法解码图像：{path}")
    return image


def save_png(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError(f"无法编码 PNG：{path}")
    encoded.tofile(str(path))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_record(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.is_file():
        raise FileNotFoundError(f"找不到输入文件：{path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path.resolve()), "sha256": digest.hexdigest()}


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到 JSON：{path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return payload


def read_rgb(path: Path) -> np.ndarray:
    return cv2.cvtColor(_imread(path, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)


def read_thermal(path: Path) -> np.ndarray:
    if not path.is_file():
        raise FileNotFoundError(f"找不到热图：{path}")
    if path.suffix.lower() == ".npy":
        raw = np.load(path, allow_pickle=False)
    else:
        raw = _imread(path, cv2.IMREAD_UNCHANGED)
    if raw.ndim != 2 or not np.issubdtype(raw.dtype, np.number):
        raise ValueError("热图必须是二维单通道数值矩阵；伪彩色截图和单点温度不能用于像素融合")
    image = raw.astype(np.float32)
    if not np.isfinite(image).any():
        raise ValueError(f"热图没有有效数值：{path}")
    return image


def read_mask(path: Path, shape: tuple[int, int] | None = None) -> np.ndarray:
    mask = _imread(path, cv2.IMREAD_UNCHANGED)
    if mask.ndim != 2 or not np.issubdtype(mask.dtype, np.integer):
        raise ValueError(f"标注必须是单通道整数 PNG：{path}")
    if shape is not None and mask.shape != shape:
        raise ValueError(f"标注尺寸 {mask.shape} 与 RGB 尺寸 {shape} 不一致：{path}")
    return mask


def parse_timestamp(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须填写带时区的 ISO 8601 时间")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 不是有效的 ISO 8601 时间：{value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区：{value}")
    return value


def read_metadata(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    for field in ("case_id", "site_id", "scene_type", "rgb_device"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"metadata 缺少 {field}：{path}")
    parse_timestamp(payload.get("captured_at"), "captured_at")
    if payload.get("thermal_captured_at"):
        parse_timestamp(payload["thermal_captured_at"], "thermal_captured_at")
    return payload


def read_event(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    for field in ("event_id", "source_url", "region", "event_type", "summary"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"event 缺少 {field}：{path}")
    if not payload["source_url"].startswith(("https://", "http://")):
        raise ValueError("event 的 source_url 必须是原始网页链接")
    parse_timestamp(payload.get("issued_at"), "issued_at")
    return payload


def homography_from_file(path: Path) -> tuple[np.ndarray, dict[str, float | int]]:
    payload = read_json(path)
    try:
        source = np.asarray(payload["source_points"], dtype=np.float32)
        target = np.asarray(payload["target_points"], dtype=np.float32)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"配准文件须包含 source_points 与 target_points：{path}") from exc
    if source.ndim != 2 or source.shape[1] != 2 or source.shape != target.shape or len(source) < 4:
        raise ValueError(f"配准至少需要 4 对对应的二维像素点：{path}")
    if not np.isfinite(source).all() or not np.isfinite(target).all():
        raise ValueError(f"配准点存在非有限数值：{path}")
    matrix, inliers = cv2.findHomography(source, target, cv2.RANSAC, 3.0)
    if matrix is None or inliers is None or int(inliers.sum()) < 4 or not np.isfinite(matrix).all():
        raise ValueError(f"无法从控制点计算可靠的配准：{path}")
    projected = cv2.perspectiveTransform(source[None, :, :], matrix)[0]
    errors = np.linalg.norm(projected - target, axis=1)
    selected = inliers.ravel().astype(bool)
    quality = {
        "control_points": int(len(source)),
        "inliers": int(selected.sum()),
        "inlier_rmse_px": float(np.sqrt(np.mean(errors[selected] ** 2))),
    }
    return matrix, quality


def warp_to(image: np.ndarray, matrix: np.ndarray, target_shape: tuple[int, int], *, nearest: bool = False) -> tuple[np.ndarray, np.ndarray]:
    height, width = target_shape
    flags = cv2.INTER_NEAREST if nearest else cv2.INTER_LINEAR
    source_valid = np.isfinite(image) if np.issubdtype(image.dtype, np.floating) else np.ones(image.shape[:2], bool)
    finite_image = np.where(source_valid, image, 0)
    warped = cv2.warpPerspective(finite_image, matrix, (width, height), flags=flags)
    valid = cv2.warpPerspective(source_valid.astype(np.uint8), matrix, (width, height), flags=cv2.INTER_NEAREST) > 0
    return warped, valid


def load_scene(rgb_path: Path, *, thermal_path: Path | None = None, thermal_registration: str | Path | None = None, mask_path: Path | None = None) -> Scene:
    rgb = read_rgb(rgb_path)
    mask = read_mask(mask_path, rgb.shape[:2]) if mask_path is not None else None
    if thermal_path is None:
        if thermal_registration:
            raise ValueError("没有热图时不能指定热图配准")
        return Scene(rgb=rgb, mask=mask)
    thermal = read_thermal(thermal_path)
    if thermal_registration == "aligned":
        if thermal.shape != rgb.shape[:2]:
            raise ValueError("标为 aligned 的热图必须与 RGB 等宽高")
        valid = np.isfinite(thermal)
        return Scene(rgb=rgb, thermal=thermal, thermal_valid=valid, mask=mask)
    if thermal_registration is None or thermal_registration == "":
        raise ValueError("独立热图必须提供配准 JSON，或经确认后显式标为 aligned")
    matrix, quality = homography_from_file(Path(thermal_registration))
    aligned, valid = warp_to(thermal, matrix, rgb.shape[:2])
    return Scene(rgb=rgb, thermal=aligned.astype(np.float32), thermal_valid=valid, mask=mask, registration_quality=quality)


def resize_scene(scene: Scene, max_edge: int) -> Scene:
    if max_edge < 64:
        raise ValueError("max_edge 至少为 64 像素")
    height, width = scene.rgb.shape[:2]
    scale = min(1.0, max_edge / max(height, width))
    if scale == 1.0:
        return scene
    target = (max(1, round(width * scale)), max(1, round(height * scale)))
    rgb = cv2.resize(scene.rgb, target, interpolation=cv2.INTER_AREA)
    thermal = cv2.resize(scene.thermal, target, interpolation=cv2.INTER_AREA) if scene.thermal is not None else None
    valid = cv2.resize(scene.thermal_valid.astype(np.uint8), target, interpolation=cv2.INTER_NEAREST) > 0 if scene.thermal_valid is not None else None
    mask = cv2.resize(scene.mask, target, interpolation=cv2.INTER_NEAREST) if scene.mask is not None else None
    return Scene(rgb=rgb, thermal=thermal, thermal_valid=valid, mask=mask, registration_quality=scene.registration_quality)
