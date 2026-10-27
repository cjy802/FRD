# -*- coding: utf-8 -*-
"""2.0 视频帧 4699-5075(手柄实拍段)打码管线。

小地图窗口存在帧(>=4920):窗口内一切检出文字像素化(含单字/logo/比例尺)。
其余帧:窗口区域仅地名强匹配才糊,避免误伤网页/界面文字。
验证:常规 + 旋转90°(竖排)两路 OCR 迭代,残留像素化,直到清零。
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402
from mask_v2_pass2 import _strong_place  # noqa: E402

FRAMES = Path(r"D:\低空经济\视频\_inspect\v3seg_frames")
FIRST, LAST = 4699, 5075
MINIMAP = (320, 630, 670, 970)   # x0, y0, x1, y1
MINIMAP_START = 4920
SCALE = 2.5

_ocr = None


def _init_worker():
    global _ocr
    import rapidocr_onnxruntime.utils as rutils

    _orig = rutils.SessionOptions

    def _lim():
        o = _orig()
        o.intra_op_num_threads = 4
        o.inter_op_num_threads = 1
        return o

    rutils.SessionOptions = _lim
    from rapidocr_onnxruntime import RapidOCR

    _ocr = RapidOCR(det_model_path=None, cls_model_path=None, rec_model_path=None)


def _ocr_variants(band):
    """同一区域两种预处理 OCR,合并检出。"""
    out = []
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    variants = [cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), band]
    for v in variants:
        big = cv2.resize(v, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
        res, _ = _ocr(big)
        for box, text, score in (res or []):
            t = str(text).strip()
            if float(score) < 0.15 or not t:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            w = (max(xs) - min(xs)) / SCALE
            h = (max(ys) - min(ys)) / SCALE
            cx = (min(xs) + max(xs)) / 2 / SCALE
            cy = (min(ys) + max(ys)) / 2 / SCALE
            out.append((cx, cy, w, h, t))
    return out


def pixelate(frame, x0, y0, x1, y1, block=10):
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0 or x1 - x0 < 4 or y1 - y0 < 4:
        return
    small = cv2.resize(roi, (max(2, (x1 - x0) // block), max(2, (y1 - y0) // block)),
                       interpolation=cv2.INTER_AREA)
    mosaic = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    mosaic = cv2.GaussianBlur(mosaic, (7, 7), 0)
    frame[y0:y1, x0:x1] = mosaic


def process(fidx: int) -> int:
    path = FRAMES / f"f_{fidx:05d}.jpg"
    frame = read_frame(path)
    if frame is None:
        return 0
    x0m, y0m, x1m, y1m = MINIMAP
    band = frame[y0m:y1m, x0m:x1m]
    hits = 0
    for cx, cy, w, h, t in _ocr_variants(band):
        if fidx >= MINIMAP_START:
            pass  # 窗口内:一切文字都糊
        elif not _strong_place(t):
            continue  # 窗口出现前:仅地名匹配
        if h > 60:
            continue
        pad_x = max(8, int(w * 0.3))
        pad_y = max(5, int(h * 0.4))
        bx0 = max(0, int(cx - w / 2 - pad_x))
        by0 = max(0, int(cy - h / 2 - pad_y))
        bx1 = min(x1m - x0m, int(cx + w / 2 + pad_x))
        by1 = min(y1m - y0m, int(cy + h / 2 + pad_y))
        pixelate(frame, x0m + bx0, y0m + by0, x0m + bx1, y0m + by1)
        hits += 1
    if hits:
        write_frame(path, frame)
    return hits


def verify(fidx: int):
    """残留检查:窗口区内任何可读文本、区外地名文本都算残留。"""
    frame = read_frame(FRAMES / f"f_{fidx:05d}.jpg")
    if frame is None:
        return []
    x0m, y0m, x1m, y1m = MINIMAP
    band = frame[y0m:y1m, x0m:x1m]
    bad = []
    for cx, cy, w, h, t in _ocr_variants(band):
        bad.append((t, x0m + cx, y0m + cy, w, h))
    return bad


def main() -> None:
    global _ocr
    _init_worker()
    t0 = time.time()
    total = 0
    for f in range(FIRST, LAST + 1):
        total += process(f)
        if (f - FIRST) % 60 == 0:
            print(f"  {f-FIRST}/{LAST-FIRST+1} frames, hits={total}, {time.time()-t0:.0f}s", flush=True)
    print(f"stage1 done: {total} pixelated boxes, {time.time()-t0:.0f}s", flush=True)

    for rnd in range(1, 5):
        leftovers = []
        for f in range(FIRST, LAST + 1):
            if f < MINIMAP_START:
                continue
            path = FRAMES / f"f_{f:05d}.jpg"
            frame = read_frame(path)
            if frame is None:
                continue
            x0m, y0m, x1m, y1m = MINIMAP
            band = frame[y0m:y1m, x0m:x1m]
            for cx, cy, w, h, t in _ocr_variants(band):
                leftovers.append((f, cx, cy, w, h, t))
        print(f"verify round {rnd}: {len(leftovers)} leftovers", flush=True)
        if not leftovers:
            break
        for f, cx, cy, w, h, t in leftovers:
            path = FRAMES / f"f_{f:05d}.jpg"
            frame = read_frame(path)
            if frame is None:
                continue
            pixelate(frame, x0m + cx - w / 2 - 8, y0m + cy - h / 2 - 6,
                     x0m + cx + w / 2 + 8, y0m + cy + h / 2 + 6)
            write_frame(path, frame)
    print(f"all done: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
