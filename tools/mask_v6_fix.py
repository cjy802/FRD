# -*- coding: utf-8 -*-
"""6.0 段修复:精确红横幅恢复 + 残留迭代像素化。

横幅检测改为限定尺寸(宽 200-900、高 60-200、y<=300)的红色轮廓,
避免右上角红色图标群的大包围盒抢占;随后对全段再跑残留验证循环。
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402
from mask_v6_segment import FRAMES_DIR, ORIG_DIR, FIRST, LAST, _init_worker, _scan_masked, blur_box


def banner_box_precise(orig):
    hsv = cv2.cvtColor(orig, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 90, 120), (10, 255, 255)) | cv2.inRange(
        hsv, (170, 90, 120), (180, 255, 255))
    mask = cv2.dilate(mask, np.ones((15, 15), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, best_area = None, 0
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if not (y <= 300 and 200 <= w <= 900 and 50 <= h <= 200):
            continue
        if w * h > best_area:
            best, best_area = (x, y, w, h), w * h
    return best


def main() -> None:
    t0 = time.time()
    # 1) 精确恢复横幅
    restored = 0
    for fidx in range(FIRST, LAST + 1):
        orig = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
        masked = read_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg")
        if orig is None or masked is None:
            continue
        box = banner_box_precise(orig)
        if box is None:
            continue
        x, y, w, h = box
        x0, y0 = max(0, x - 6), max(0, y - 6)
        x1, y1 = min(orig.shape[1], x + w + 6), min(orig.shape[0], y + h + 6)
        masked[y0:y1, x0:x1] = orig[y0:y1, x0:x1]
        write_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg", masked)
        restored += 1
    print(f"banner precisely restored on {restored} frames", flush=True)

    # 2) 残留验证迭代(横幅文字属 UI,不会被报)
    _init_worker()
    for rnd in range(1, 4):
        leftovers = []
        for fidx in range(FIRST, LAST + 1):
            for cx, cy, w, h, t in _scan_masked(fidx)[1]:
                leftovers.append((fidx, cx, cy, w, h, t))
        print(f"verify round {rnd}: {len(leftovers)} leftovers", flush=True)
        if not leftovers:
            break
        by_frame: dict[int, list] = {}
        for fidx, cx, cy, w, h, t in leftovers:
            by_frame.setdefault(fidx, []).append((cx, cy, w, h, t))
        for fidx, items in by_frame.items():
            frame = read_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg")
            if frame is None:
                continue
            for cx, cy, w, h, t in items:
                blur_box(frame, cx, cy, w, h, t)
            write_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg", frame)
    print(f"fix done: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
