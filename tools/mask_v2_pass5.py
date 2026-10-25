# -*- coding: utf-8 -*-
"""v2 段打码第五轮:校名/地标聚类框加宽重糊,覆盖被遮挡标签的残字。"""
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
ORIG_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_orig")
FIRST, LAST = 5105, 5311
SCORE_MIN = 0.2
MIN_CLUSTER_HITS = 10
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


def _detect(fidx: int):
    path = ORIG_DIR / f"f_{fidx:05d}.jpg"
    frame = read_frame(path)
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
    if res:
        for box, text, score in res:
            t = str(text).strip()
            if float(score) < SCORE_MIN or not _strong_place(t):
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            w = (max(xs) - min(xs)) / SCALE
            h = (max(ys) - min(ys)) / SCALE
            if not (8 <= min(w, h) / (len(t) or 1) * SCALE <= 60):
                continue
            cx = (min(xs) + max(xs)) / 2 / SCALE + x0c
            cy = (min(ys) + max(ys)) / 2 / SCALE + y0c
            found.append((cx, cy, w, h, t))
    return fidx, found


WIDE_WORDS = ("大学", "学院", "学校", "教学楼", "职业技术")


def main() -> None:
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=7, initializer=_init_worker) as ex:
        per_frame = list(ex.map(_detect, range(FIRST, LAST + 1)))

    clusters: list[dict] = []
    for fidx, found in per_frame:
        for cx, cy, w, h, t in found:
            best = None
            for c in clusters:
                if abs(cx - c["cx"]) < 60 and abs(cy - c["cy"]) < 30:
                    best = c
                    break
            if best is None:
                best = {"cx": 0.0, "cy": 0.0, "frames": {}, "wide": False}
                clusters.append(best)
            best["frames"][fidx] = (cx, cy, w, h)
            n = len(best["frames"])
            best["cx"] += (cx - best["cx"]) / n
            best["cy"] += (cy - best["cy"]) / n
            if any(word in t for word in WIDE_WORDS):
                best["wide"] = True

    stable = [c for c in clusters if len(c["frames"]) >= MIN_CLUSTER_HITS]
    print(f"clusters: {len(clusters)} total, {len(stable)} stable, "
          f"{sum(1 for c in stable if c['wide'])} wide (school-like)")

    total = 0
    for fidx, _ in per_frame:
        tasks = []
        for c in stable:
            if not c["frames"]:
                continue
            nearest = min(c["frames"], key=lambda f: abs(f - fidx))
            if abs(nearest - fidx) > 60:
                continue  # 太远,位置不可信
            cx, cy, w, h = c["frames"][nearest]
            pad_x = max(16, int(w * 0.9 if c["wide"] else w * 0.5))
            pad_y = max(4, int(h * 0.6))
            tasks.append((cx, cy, w, h, pad_x, pad_y))
        if not tasks:
            continue
        path = FRAMES_DIR / f"f_{fidx:05d}.jpg"
        frame = read_frame(path)
        if frame is None:
            continue
        h_img, w_img = frame.shape[:2]
        hit = False
        for cx, cy, w, h, pad_x, pad_y in tasks:
            x0 = max(0, int(cx - w / 2 - pad_x))
            y0 = max(0, int(cy - h / 2 - pad_y))
            x1 = min(w_img, int(cx + w / 2 + pad_x))
            y1 = min(h_img, int(cy + h / 2 + pad_y))
            k = int(max(17, (y1 - y0) * 0.5))
            if k % 2 == 0:
                k += 1
            roi = frame[y0:y1, x0:x1]
            if roi.size == 0:
                continue
            blurred = cv2.GaussianBlur(roi, (k, k), 0)
            blurred = cv2.GaussianBlur(blurred, (k, k), 0)
            frame[y0:y1, x0:x1] = blurred
            total += 1
            hit = True
        if hit:
            write_frame(path, frame)
    print(f"pass5 done: {total} widened patches, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
