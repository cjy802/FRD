# -*- coding: utf-8 -*-
"""6.0 总片帧 4699-5075(手柄+大地图段)终版打码管线。

阶段1: 原始帧全量文字聚类;类内文本全部为纯 UI 的跳过,其余像素化。
阶段2: 三路验证(常规/旋转竖排/底边带)迭代,残留像素化,直到清零。
阶段3: 从原始帧恢复红横幅与纯 UI 元素(耗时距离/航点/兴趣点/下一步/高德logo)。
"""
import re
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402
from mask_v2_pass2 import _strong_place  # noqa: E402

FRAMES_DIR = Path(r"D:\低空经济\视频\_inspect\v6seg_frames")
ORIG_DIR = Path(r"D:\低空经济\视频\_inspect\v6seg_orig")
FIRST, LAST = 4699, 5075
FPS = 30.0
SCORE_MIN = 0.2
SCALE = 1.6
SUBTITLE_Y = 1005     # 字幕带起始(6.0 为 1080 高,字幕约 1010-1075)

UI_EXACT = {"航点", "兴趣点", "下一步", "N/A", "飞机未连接上遥控器",
            "3.航线在线导入", "高德地图", "开启航点飞行"}
DIGIT_UI = re.compile(r"^[HhRnN0-9.,OolIiUmM]{1,10}$")

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


def is_ui_text(t: str) -> bool:
    if t in UI_EXACT or "耗时" in t or "距离" in t:
        return True
    if _strong_place(t):
        return False
    if DIGIT_UI.match(t):
        return True
    return False


def detect(frame):
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
        cx = (min(xs) + max(xs)) / 2 / SCALE
        cy = (min(ys) + max(ys)) / 2 / SCALE
        found.append((cx, cy, w, h, t))
    return found


def _detect_orig(fidx: int):
    frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
    if frame is None:
        return fidx, []
    return fidx, detect(frame)


def _scan_masked(fidx: int):
    frame = read_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg")
    if frame is None:
        return fidx, []
    out = []
    for cx, cy, w, h, t in detect(frame):
        if cy > SUBTITLE_Y:
            continue
        out.append((cx, cy, w, h, t))
    return fidx, out


def pixelate(frame, x0, y0, x1, y1, block=12):
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    x0, y0 = max(0, x0), max(0, y0)
    h_img, w_img = frame.shape[:2]
    x1, y1 = min(w_img, x1), min(h_img, y1)
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0 or x1 - x0 < 4 or y1 - y0 < 4:
        return
    small = cv2.resize(roi, (max(2, (x1 - x0) // block), max(2, (y1 - y0) // block)),
                       interpolation=cv2.INTER_AREA)
    mosaic = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    mosaic = cv2.GaussianBlur(mosaic, (7, 7), 0)
    frame[y0:y1, x0:x1] = mosaic


def blur_box(frame, cx, cy, w, h, t: str):
    ch = max(9.0, h)
    pad_x = max(14, int(w * 0.9 if any(x in t for x in ("大学", "学院", "学校", "职业技术")) else w * 0.45))
    pad_y = max(5, int(h * 0.55))
    pixelate(frame, cx - w / 2 - pad_x, cy - h / 2 - pad_y,
             cx + w / 2 + pad_x, cy + h / 2 + pad_y)


def restore_banner(masked, orig):
    hsv = cv2.cvtColor(orig, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 90, 120), (10, 255, 255)) | cv2.inRange(
        hsv, (170, 90, 120), (180, 255, 255))
    mask = cv2.dilate(mask, np.ones((25, 25), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, best_area = None, 0
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if y > 400 or w < 150:
            continue
        if w * h > best_area:
            best, best_area = (x, y, w, h), w * h
    if best is None:
        return
    x, y, w, h = best
    x0, y0 = max(0, x - 6), max(0, y - 6)
    x1, y1 = min(orig.shape[1], x + w + 6), min(orig.shape[0], y + h + 6)
    masked[y0:y1, x0:x1] = orig[y0:y1, x0:x1]


def main() -> None:
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=7, initializer=_init_worker) as ex:
        per_frame = list(ex.map(_detect_orig, range(FIRST, LAST + 1)))

    clusters: list[dict] = []
    for fidx, found in per_frame:
        for cx, cy, w, h, t in found:
            best = None
            for c in clusters:
                if abs(cx - c["cx"]) < 60 and abs(cy - c["cy"]) < 30:
                    best = c
                    break
            if best is None:
                best = {"cx": 0.0, "cy": 0.0, "frames": {}, "ui": True}
                clusters.append(best)
            best["frames"][fidx] = (cx, cy, w, h, t)
            n = len(best["frames"])
            best["cx"] += (cx - best["cx"]) / n
            best["cy"] += (cy - best["cy"]) / n
            if not is_ui_text(t):
                best["ui"] = False

    targets = [c for c in clusters if not c["ui"] and len(c["frames"]) >= 3]
    skipped = [c for c in clusters if c["ui"] and len(c["frames"]) >= 3]
    print(f"clusters: {len(clusters)} total, {len(targets)} to blur, "
          f"{len(skipped)} UI kept", flush=True)

    for fidx, _ in per_frame:
        frame = None
        for c in targets:
            nearest = min(c["frames"], key=lambda f: abs(f - fidx))
            if abs(nearest - fidx) > 60:
                continue
            cx, cy, w, h, t = c["frames"][nearest]
            if frame is None:
                frame = read_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg")
                if frame is None:
                    break
            blur_box(frame, cx, cy, w, h, t)
        if frame is not None:
            write_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg", frame)
    print(f"stage1 done: {time.time()-t0:.0f}s", flush=True)

    for rnd in range(1, 4):
        leftovers = []
        with ProcessPoolExecutor(max_workers=7, initializer=_init_worker) as ex:
            for fidx, found in ex.map(_scan_masked, range(FIRST, LAST + 1)):
                for cx, cy, w, h, t in found:
                    if not is_ui_text(t):
                        leftovers.append((fidx, cx, cy, w, h, t))
        print(f"verify round {rnd}: {len(leftovers)} leftovers", flush=True)
        if not leftovers:
            break
        by_frame: dict[int, list] = {}
        for item in leftovers:
            by_frame.setdefault(item[0], []).append(item[1:])
        for fidx, items in by_frame.items():
            frame = read_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg")
            if frame is None:
                continue
            for cx, cy, w, h, t in items:
                blur_box(frame, cx, cy, w, h, t)
            write_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg", frame)

    restored = 0
    for fidx in range(FIRST, LAST + 1):
        orig = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
        masked = read_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg")
        if orig is None or masked is None:
            continue
        restore_banner(masked, orig)
        write_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg", masked)
        restored += 1
    print(f"all done: banner restored on {restored} frames, {time.time()-t0:.0f}s total")


if __name__ == "__main__":
    main()
