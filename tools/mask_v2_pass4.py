# -*- coding: utf-8 -*-
"""v2 段打码第四轮:多帧位置投票。

扫描全部帧收集地名检出位置并聚类;在多数帧稳定出现、但当前帧漏检的
地标,用聚类平均位置补糊(手持抖动幅度小,平均位置 + 扩边足够覆盖)。
"""
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402
from mask_v2_pass2 import _strong_place  # noqa: E402

FRAMES_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_frames")   # 已打码帧,补糊的目标
ORIG_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_orig")       # 原始帧,用于检测收集
FIRST, LAST = 5105, 5311
SCORE_MIN = 0.2
MIN_CLUSTER_HITS = 12   # 至少出现在这么多原始帧才算稳定地标
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
            found.append((cx, cy, w, h))
    return fidx, found


def main() -> None:
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=7, initializer=_init_worker) as ex:
        per_frame = list(ex.map(_detect, range(FIRST, LAST + 1)))

    # 贪心聚类:中心距离近的检出归为一类
    clusters: list[dict] = []
    for fidx, found in per_frame:
        for cx, cy, w, h in found:
            best = None
            for c in clusters:
                if abs(cx - c["cx"]) < 60 and abs(cy - c["cy"]) < 30:
                    best = c
                    break
            if best is None:
                best = {"cx": 0.0, "cy": 0.0, "frames": {}}
                clusters.append(best)
            best["frames"][fidx] = (cx, cy, w, h)
            n = len(best["frames"])
            best["cx"] += (cx - best["cx"]) / n
            best["cy"] += (cy - best["cy"]) / n

    stable = [c for c in clusters if len(c["frames"]) >= MIN_CLUSTER_HITS]
    print(f"clusters: {len(clusters)} total, {len(stable)} stable (>= {MIN_CLUSTER_HITS} frames)")

    total = 0
    for fidx, _ in per_frame:
        tasks = []
        for si, c in enumerate(stable):
            if fidx in c["frames"]:
                continue  # 该帧本身检出并已由前三轮处理
            # 用时间上最近的检出帧位置跟踪手持抖动
            nearest = min(c["frames"], key=lambda f: abs(f - fidx))
            tasks.append(c["frames"][nearest])
        if not tasks:
            continue
        path = FRAMES_DIR / f"f_{fidx:05d}.jpg"
        frame = read_frame(path)
        if frame is None:
            continue
        h_img, w_img = frame.shape[:2]
        hit = False
        for cx, cy, w, h in tasks:
            ch = max(10.0, h)
            pad_x = max(14, int(w * 0.4))
            pad_y = max(4, int(h * 0.5))
            x0 = max(0, int(cx - w / 2 - pad_x))
            y0 = max(0, int(cy - h / 2 - pad_y))
            x1 = min(w_img, int(cx + w / 2 + pad_x))
            y1 = min(h_img, int(cy + h / 2 + pad_y))
            k = int(max(ch * 1.8, 17))
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
    print(f"pass4 done: {total} voted patches, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
