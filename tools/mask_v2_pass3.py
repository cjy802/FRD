# -*- coding: utf-8 -*-
"""v2 段打码第三轮:屏幕区域放大 + 对比度增强后 OCR,抓低质量小字。"""
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402
from mask_v2_pass2 import _strong_place  # noqa: E402

FRAMES_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_frames")
FIRST, LAST = 5105, 5311
SCORE_MIN = 0.2
SCALE = 1.6
# 遥控器屏幕在画面中的大致范围(留边距)
CROP = (200, 100, 1830, 1030)  # x0, y0, x1, y1

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


def _enhance(band):
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _process_frame(fidx: int):
    path = FRAMES_DIR / f"f_{fidx:05d}.jpg"
    frame = read_frame(path)
    if frame is None:
        return fidx, 0
    x0c, y0c, x1c, y1c = CROP
    band = frame[y0c:y1c, x0c:x1c]
    big = cv2.resize(_enhance(band), None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
    res, _ = _ocr(big)
    hits = 0
    if res:
        h_img, w_img = frame.shape[:2]
        for box, text, score in res:
            t = str(text).strip()
            if float(score) < SCORE_MIN or not _strong_place(t):
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            ch_big = min(max(xs) - min(xs), max(ys) - min(ys)) / (len(t) or 1)
            ch = max(8.0, ch_big / SCALE)
            w = (max(xs) - min(xs)) / SCALE
            h = (max(ys) - min(ys)) / SCALE
            x = min(xs) / SCALE + x0c
            y = min(ys) / SCALE + y0c
            pad_x = max(12, int(w * 0.35))
            if any(word in t for word in ("大学", "学院", "学校", "教学楼")):
                pad_x += int(w * 0.5)
            pad_y = max(2, int(ch * 0.5))
            bx0 = max(0, int(x - pad_x))
            by0 = max(0, int(y - pad_y))
            bx1 = min(w_img, int(x + w + pad_x))
            by1 = min(h_img, int(y + h + pad_y))
            k = int(max(ch * 1.8, 15))
            if k % 2 == 0:
                k += 1
            roi = frame[by0:by1, bx0:bx1]
            if roi.size == 0:
                continue
            blurred = cv2.GaussianBlur(roi, (k, k), 0)
            blurred = cv2.GaussianBlur(blurred, (k, k), 0)
            frame[by0:by1, bx0:bx1] = blurred
            hits += 1
    if hits:
        write_frame(path, frame)
    return fidx, hits


def main() -> None:
    t0 = time.time()
    total = 0
    with ProcessPoolExecutor(max_workers=7, initializer=_init_worker) as ex:
        for i, (fidx, hits) in enumerate(
            ex.map(_process_frame, range(FIRST, LAST + 1)), 1
        ):
            total += hits
            if i % 50 == 0:
                print(f"  {i}/{LAST - FIRST + 1}, hits={total}, {time.time()-t0:.0f}s", flush=True)
    print(f"pass3 done: {total} extra regions, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
