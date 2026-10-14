"""Simple, inspectable pixel features; no claim that reflection proves water."""

from __future__ import annotations

import cv2
import numpy as np

from .config import MODES
from .images import Scene


def feature_stack(scene: Scene, mode: str) -> tuple[np.ndarray, np.ndarray]:
    if mode not in MODES:
        raise ValueError(f"未知模式：{mode}")
    height, width = scene.rgb.shape[:2]
    parts: list[np.ndarray] = []
    valid = np.ones((height, width), dtype=bool)
    if mode in ("rgb", "fusion"):
        rgb = scene.rgb.astype(np.float32) / 255.0
        hsv = cv2.cvtColor(scene.rgb, cv2.COLOR_RGB2HSV).astype(np.float32)
        hsv /= np.array([179.0, 255.0, 255.0], dtype=np.float32)
        gray = cv2.cvtColor(scene.rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
        mean = cv2.GaussianBlur(gray, (7, 7), 0)
        square_mean = cv2.GaussianBlur(gray * gray, (7, 7), 0)
        texture = np.sqrt(np.maximum(square_mean - mean * mean, 0))
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        edges = np.clip(cv2.magnitude(gx, gy), 0, 1)
        # Bright, low-saturation, locally smooth pixels are only a reflection cue.
        reflection_cue = hsv[:, :, 2] * (1 - hsv[:, :, 1]) * np.exp(-10 * texture)
        parts.extend((rgb, hsv, texture[:, :, None], edges[:, :, None], reflection_cue[:, :, None]))
    if mode in ("thermal", "fusion"):
        if scene.thermal is None or scene.thermal_valid is None:
            raise ValueError(f"{mode} 模式需要已配准的二维热图")
        valid &= scene.thermal_valid
        observed = scene.thermal[valid]
        if observed.size == 0:
            raise ValueError("RGB 与热图没有可用的重叠像素")
        median = float(np.median(observed))
        p05, p95 = np.percentile(observed, [5, 95])
        spread = max(float(p95 - p05), 1e-6)
        thermal = np.where(valid, scene.thermal, median)
        relative = np.clip((thermal - median) / spread, -3, 3).astype(np.float32)
        local_mean = cv2.GaussianBlur(relative, (11, 11), 0)
        local_contrast = relative - local_mean
        local_sq_mean = cv2.GaussianBlur(relative * relative, (11, 11), 0)
        local_std = np.sqrt(np.maximum(local_sq_mean - local_mean * local_mean, 0))
        parts.extend((relative[:, :, None], local_contrast[:, :, None], local_std[:, :, None]))
    return np.concatenate(parts, axis=2).astype(np.float32), valid
