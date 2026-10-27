# -*- coding: utf-8 -*-
"""6.0 段时域聚合打码:滑窗 31 帧内的检出框共享给窗口内每一帧,
生成聚合后的糊框 JSON,再由 make_v6_masked_video.py 消费重新合成。"""
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame  # noqa: E402
from mask_v6_segment import FIRST, LAST, _init_worker, is_ui_text  # noqa: E402

ORIG_DIR = Path(r"D:\低空经济\视频\_inspect\v6seg_orig")
OUT_JSON = Path(r"D:\低空经济\output\v6seg_blur_boxes2.json")
START = 4920          # 小地图/大地图出现的起始帧
WINDOW = 31           # 时域聚合窗口(±15 帧)
SCORE_MIN = 0.15
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


def detect_raw(fidx: int):
    frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
    if frame is None:
        for _ in range(5):
            time.sleep(0.8)
            frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
            if frame is not None:
                break
    if frame is None:
        return fidx, []
    x0c, y0c = 0, 0
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    big = cv2.resize(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), None,
                     fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
    res, _ = _ocr(big)
    found = []
    for box, text, score in (res or []):
        t = str(text).strip()
        if float(score) < SCORE_MIN or not t:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        w = (max(xs) - min(xs)) / SCALE
        h = (max(ys) - min(ys)) / SCALE
        if h > 60:
            continue
        cx = (min(xs) + max(xs)) / 2 / SCALE + x0c
        cy = (min(ys) + max(ys)) / 2 / SCALE + y0c
        found.append((cx, cy, w, h, t))
    return fidx, found


def main() -> None:
    t0 = time.time()
    per_frame: dict[int, list] = {}
    with ProcessPoolExecutor(max_workers=7, initializer=_init) as ex:
        for fidx, found in ex.map(detect_raw, range(START, LAST + 1)):
            per_frame[fidx] = found
    print(f"detect done: {sum(len(v) for v in per_frame.values())} raw boxes, "
          f"{time.time()-t0:.0f}s", flush=True)

    # 时域聚合:每帧取 [f-15, f+15] 窗口内的全部检出(过滤 UI),按位置去重
    out: dict[int, list] = {}
    for fidx in range(START, LAST + 1):
        window = []
        seen_xy = []
        for f in range(max(START, fidx - 15), min(LAST, fidx + 15) + 1):
            for cx, cy, w, h, t in per_frame.get(f, []):
                if is_ui_text(t):
                    continue
                if any(abs(cx - sx) < 40 and abs(cy - sy) < 25 for sx, sy in seen_xy):
                    continue
                seen_xy.append((cx, cy))
                window.append((cx, cy, w, h, t))
        out[fidx] = window
    n_boxes = sum(len(v) for v in out.values())
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"aggregated: {n_boxes} boxes over {len(out)} frames -> {OUT_JSON}, "
          f"{time.time()-t0:.0f}s total")


if __name__ == "__main__":
    main()
