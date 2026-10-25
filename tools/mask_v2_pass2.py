# -*- coding: utf-8 -*-
"""v2 段打码补漏:低置信度黑名单词 + 命中框横向扩展,覆盖被遮挡标签的残留。"""
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import mask_map_labels as base  # noqa: E402
from mask_map_labels import char_height, read_frame, write_frame  # noqa: E402

FRAMES_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_frames")
FIRST, LAST = 5105, 5311
SCORE_MIN = 0.25

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


def _strong_place(text: str) -> bool:
    """只信黑名单子串与强模式,避免低阈值带来的误伤。"""
    t = text.strip()
    if not t or t in base.UI_WHITELIST:
        return False
    if any(name in t for name in base.PLACE_NAMES):
        return True
    return base._looks_like_place_pattern(t)


def _blur(frame, x0, y0, x1, y1, char_h: float):
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    k = int(max(char_h * 1.8, 15))
    if k % 2 == 0:
        k += 1
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return
    blurred = cv2.GaussianBlur(roi, (k, k), 0)
    blurred = cv2.GaussianBlur(blurred, (k, k), 0)
    frame[y0:y1, x0:x1] = blurred


def _process_frame(fidx: int):
    path = FRAMES_DIR / f"f_{fidx:05d}.jpg"
    frame = read_frame(path)
    if frame is None:
        return fidx, 0
    res, _ = _ocr(frame)
    hits = 0
    if res:
        h_img, w_img = frame.shape[:2]
        for box, text, score in res:
            t = text.strip()
            if float(score) < SCORE_MIN or not _strong_place(t):
                continue
            ch, x, y, w, h = char_height(box)
            if ch < 6 or ch > 60:
                continue
            # 横向扩宽:遮挡标签的残留通常在检出框两侧
            pad_x = max(12, int(w * 0.35))
            if any(word in t for word in ("大学", "学院", "学校", "教学楼")):
                pad_x += int(w * 0.5)
            pad_y = max(2, int(ch * 0.4))
            x0 = max(0, x - pad_x)
            y0 = max(0, y - pad_y)
            x1 = min(w_img, x + w + pad_x)
            y1 = min(h_img, y + h + pad_y)
            _blur(frame, x0, y0, x1, y1, ch)
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
    print(f"pass2 done: {total} extra regions, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
