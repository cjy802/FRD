# -*- coding: utf-8 -*-
"""v2 段收尾:把红色"飞机未连接上遥控器"横幅从原始帧恢复,撤销误盖的模糊。

横幅为纯 UI 提示、无地名,直接按红色区域检测包围盒从原始帧拷贝还原;
横幅以外的打码内容全部保留。
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402

FRAMES_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_frames")
ORIG_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_orig")
FIRST, LAST = 5105, 5311


def banner_box(orig):
    hsv = cv2.cvtColor(orig, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 90, 120), (10, 255, 255)) | cv2.inRange(
        hsv, (170, 90, 120), (180, 255, 255))
    mask = cv2.dilate(mask, np.ones((25, 25), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, best_area = None, 0
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if y > 400 or w < 200:   # 横幅只出现在画面上部
            continue
        if w * h > best_area:
            best, best_area = (x, y, w, h), w * h
    return best


def main() -> None:
    t0 = time.time()
    restored = 0
    for fidx in range(FIRST, LAST + 1):
        orig = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
        masked = read_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg")
        if orig is None or masked is None:
            continue
        box = banner_box(orig)
        if box is None:
            continue
        x, y, w, h = box
        pad = 6
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(orig.shape[1], x + w + pad), min(orig.shape[0], y + h + pad)
        masked[y0:y1, x0:x1] = orig[y0:y1, x0:x1]
        write_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg", masked)
        restored += 1
    print(f"banner restored on {restored} frames, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
