# -*- coding: utf-8 -*-
"""在 6.0 总片中定位 2.0 段(帧 4699-5075)的对应时间位置。"""
import sys
from pathlib import Path

import cv2
import numpy as np

V6 = Path(r"D:\低空经济\视频\完成_10.27_6.0.mp4")
ORIG = Path(r"D:\低空经济\视频\_inspect\v3seg_orig")
QUERIES = [4700, 4900, 5070]   # 2.0 段内帧号(1-based)
FPS = 30.0


def thumb_2d0(frame):
    """2.0 1986 宽 -> 中心裁 1916 -> 缩略 96x54 灰度。"""
    h, w = frame.shape[:2]
    x0 = (w - 1916) // 2
    crop = frame[:, x0:x0 + 1916]
    g = cv2.resize(crop, (96, 54))
    return cv2.cvtColor(g, cv2.COLOR_BGR2GRAY).astype(np.float32)


def main() -> None:
    cap = cv2.VideoCapture(str(V6))
    samples = []
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % 15 == 0:   # 每 0.5s 采样
            g = cv2.resize(frame, (96, 54))
            samples.append((idx, cv2.cvtColor(g, cv2.COLOR_BGR2GRAY).astype(np.float32)))
        idx += 1
    cap.release()
    print(f"6.0 frames: {idx}, samples: {len(samples)}")

    results = []
    for qf in QUERIES:
        p = ORIG / f"f_{qf:05d}.jpg"
        img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), 1)
        if img is None:
            continue
        q = thumb_2d0(img)
        best = min(samples, key=lambda s: float(np.abs(s[1] - q).mean()))
        mad = float(np.abs(best[1] - q).mean())
        results.append((qf, best[0], round(mad, 2)))
        print(f"query frame {qf} -> best 6.0 frame {best[0]} (t={best[0]/FPS:.2f}s) mad={mad:.2f}")

    if len(results) >= 2:
        offsets = [f6 - (qf - 1) for qf, f6, _ in results]
        print("offsets (6.0 frame - 2.0 frame + 1):", offsets)


if __name__ == "__main__":
    main()
