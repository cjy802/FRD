# -*- coding: utf-8 -*-
"""6.0 段小地图窗口逐文字像素化:在原始帧上检测窗口内全部文字(含单字),
应用到已打码帧的对应位置,彻底覆盖校名残字。"""
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402

ORIG_DIR = Path(r"D:\低空经济\视频\_inspect\v6seg_orig")
FRAMES_DIR = Path(r"D:\低空经济\视频\_inspect\v6seg_frames")
FIRST, LAST = 4920, 5075
WIN = (320, 630, 670, 970)   # x0, y0, x1, y1
SCALE = 2.5

_ocr = None


def _init():
    global _ocr
    import rapidocr_onnxruntime.utils as rutils

    orig = rutils.SessionOptions

    def lim():
        o = orig()
        o.intra_op_num_threads = 4
        o.inter_op_num_threads = 1
        return o

    rutils.SessionOptions = lim
    from rapidocr_onnxruntime import RapidOCR

    _ocr = RapidOCR(det_model_path=None, cls_model_path=None, rec_model_path=None)


def window_boxes(fidx: int):
    frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
    if frame is None:
        for _ in range(5):
            time.sleep(0.8)
            frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
            if frame is not None:
                break
    if frame is None:
        return fidx, []
    x0, y0, x1, y1 = WIN
    band = frame[y0:y1, x0:x1]
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    big = cv2.resize(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), None,
                     fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
    res, _ = _ocr(big)
    boxes = []
    for box, text, score in (res or []):
        t = str(text).strip()
        if float(score) < 0.1 or not t:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        w = (max(xs) - min(xs)) / SCALE
        h = (max(ys) - min(ys)) / SCALE
        cx = (min(xs) + max(xs)) / 2 / SCALE + x0
        cy = (min(ys) + max(ys)) / 2 / SCALE + y0
        boxes.append((cx, cy, w, h))
    return fidx, boxes


def main() -> None:
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=7, initializer=_init) as ex:
        per_frame = dict(ex.map(window_boxes, range(FIRST, LAST + 1)))

    total = 0
    for fidx in range(FIRST, LAST + 1):
        boxes = per_frame.get(fidx)
        if not boxes:
            continue
        p = FRAMES_DIR / f"f_{fidx:05d}.jpg"
        frame = read_frame(p)
        if frame is None:
            continue
        for cx, cy, w, h in boxes:
            pad_x = max(10, int(w * 0.35))
            pad_y = max(6, int(h * 0.35))
            x0 = max(0, int(cx - w / 2 - pad_x))
            y0 = max(0, int(cy - h / 2 - pad_y))
            x1 = int(cx + w / 2 + pad_x)
            y1 = int(cy + h / 2 + pad_y)
            roi = frame[y0:y1, x0:x1]
            if roi.size == 0:
                continue
            small = cv2.resize(roi, (max(2, (x1 - x0) // 12), max(2, (y1 - y0) // 12)),
                               interpolation=cv2.INTER_AREA)
            mosaic = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
            mosaic = cv2.GaussianBlur(mosaic, (7, 7), 0)
            frame[y0:y1, x0:x1] = mosaic
            total += 1
        write_frame(p, frame)
    print(f"window pass done: {total} boxes pixelated on {LAST-FIRST+1} frames, "
          f"{time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
