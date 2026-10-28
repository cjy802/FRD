# -*- coding: utf-8 -*-
"""v2 段收尾:从原始帧恢复被误糊的纯 UI 元素(兴趣点按钮、耗时距离牌、
下一步按钮、高德地图 logo),并再次恢复红色横幅。地名打码保持不变。"""
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402

FRAMES_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_frames")
ORIG_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_orig")
FIRST, LAST = 5105, 5311
CROP = (200, 100, 1830, 1030)
SCALE = 1.6

_ocr = None


def _init_worker():
    global _ocr
    import rapidocr_onnxruntime.utils as rutils

    _orig_options = rutils.SessionOptions

    def _limited_options():
        opt = _orig_options()
        opt.intra_op_num_threads = 4
        opt.inter_op_num_threads = 1
        return opt

    rutils.SessionOptions = _limited_options
    from rapidocr_onnxruntime import RapidOCR

    _ocr = RapidOCR(det_model_path=None, cls_model_path=None, rec_model_path=None)


def is_restore_text(t: str) -> bool:
    if any(w in t for w in ("耗时", "距离", "兴趣", "下一步")):
        return True
    return t.startswith("的图") or "高德" in t


def _detect(fidx: int):
    frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
    if frame is None:
        return fidx, []
    x0c, y0c, x1c, y1c = CROP
    band = frame[y0c:y1c, x0c:x1c]
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    big = cv2.resize(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), None,
                     fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
    res, _ = _ocr(big)
    found = []
    for box, text, score in (res or []):
        t = str(text).strip()
        if not is_restore_text(t):
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        w = (max(xs) - min(xs)) / SCALE
        h = (max(ys) - min(ys)) / SCALE
        cx = (min(xs) + max(xs)) / 2 / SCALE + x0c
        cy = (min(ys) + max(ys)) / 2 / SCALE + y0c
        found.append((cx, cy, w, h))
    return fidx, found


def banner_box(orig):
    hsv = cv2.cvtColor(orig, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 90, 120), (10, 255, 255)) | cv2.inRange(
        hsv, (170, 90, 120), (180, 255, 255))
    mask = cv2.dilate(mask, np.ones((25, 25), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, best_area = None, 0
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if y > 400 or w < 200:
            continue
        if w * h > best_area:
            best, best_area = (x, y, w, h), w * h
    return best


def main() -> None:
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=7, initializer=_init_worker) as ex:
        per_frame = list(ex.map(_detect, range(FIRST, LAST + 1)))

    clusters: list[dict] = []
    for fidx, found in per_frame:
        for cx, cy, w, h in found:
            best = None
            for c in clusters:
                if abs(cx - c["cx"]) < 70 and abs(cy - c["cy"]) < 35:
                    best = c
                    break
            if best is None:
                best = {"cx": 0.0, "cy": 0.0, "frames": {}}
                clusters.append(best)
            best["frames"][fidx] = (cx, cy, w, h)
            n = len(best["frames"])
            best["cx"] += (cx - best["cx"]) / n
            best["cy"] += (cy - best["cy"]) / n

    restored = 0
    for fidx in range(FIRST, LAST + 1):
        orig = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
        masked = read_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg")
        if orig is None or masked is None:
            continue
        h_img, w_img = masked.shape[:2]
        # UI 元素:用时间上最近的检出位置从原始帧拷贝还原
        for c in clusters:
            if not c["frames"]:
                continue
            nearest = min(c["frames"], key=lambda f: abs(f - fidx))
            if abs(nearest - fidx) > 45:
                continue
            cx, cy, w, h = c["frames"][nearest]
            x0 = max(0, int(cx - w / 2 - 10))
            y0 = max(0, int(cy - h / 2 - 8))
            x1 = min(w_img, int(cx + w / 2 + 10))
            y1 = min(h_img, int(cy + h / 2 + 8))
            masked[y0:y1, x0:x1] = orig[y0:y1, x0:x1]
        # 红横幅
        box = banner_box(orig)
        if box is not None:
            x, y, w, h = box
            x0, y0 = max(0, x - 6), max(0, y - 6)
            x1, y1 = min(w_img, x + w + 6), min(h_img, y + h + 6)
            masked[y0:y1, x0:x1] = orig[y0:y1, x0:x1]
        write_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg", masked)
        restored += 1
    print(f"pass9 done: UI restored on {restored} frames, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
