# -*- coding: utf-8 -*-
"""v2 段终版打码:原始帧全量文字聚类,非 UI 文本全部糊掉,迭代验证到清零。

阶段1: 在原始帧上 OCR 收集全部文字位置并聚类;类内文本全部为纯 UI
      (耗时/距离/高度牌/按钮/横幅)的跳过,其余(地名、残字、无法分类)全糊。
阶段2: 对打码结果迭代扫描,凡仍有非 UI 可读文字立即补糊,直到清零(最多3轮)。
阶段3: 从原始帧恢复红色横幅。
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

FRAMES_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_frames")
ORIG_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_orig")
FIRST, LAST = 5105, 5311
SCORE_MIN = 0.2
CROP = (200, 100, 1830, 1030)
SCALE = 1.6
BANNER_RESTORE_MIN_Y = 400

UI_EXACT = {"航点", "兴趣点", "下一步", "3.飞行路线导入", "飞机未连接上遥控器", "音乐喷泉"}
DIGIT_UI = re.compile(r"^[HhRnN0-9.,OolIiUmM]{1,10}$")


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


def _detect(frame):
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
    return found


def _detect_orig(fidx: int):
    frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
    if frame is None:
        return fidx, []
    return fidx, _detect(frame)


def _scan_masked(fidx: int):
    frame = read_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg")
    if frame is None:
        return fidx, []
    out = []
    for cx, cy, w, h, t in _detect(frame):
        if cy > 990:   # 字幕带
            continue
        out.append((cx, cy, w, h, t))
    return fidx, out


def is_ui_text(t: str) -> bool:
    if t in UI_EXACT or "耗时" in t or "距离" in t:
        return True
    if _strong_place(t):
        return False   # 地名类永不算 UI
    if DIGIT_UI.match(t):
        return True    # 高度/距离数字标牌
    return False


def blur_box(frame, cx, cy, w, h, t: str):
    ch = max(9.0, h)
    pad_x = max(14, int(w * 0.9 if any(x in t for x in ("大学", "学院", "学校", "职业技术")) else w * 0.45))
    pad_y = max(4, int(h * 0.55))
    h_img, w_img = frame.shape[:2]
    x0 = max(0, int(cx - w / 2 - pad_x))
    y0 = max(0, int(cy - h / 2 - pad_y))
    x1 = min(w_img, int(cx + w / 2 + pad_x))
    y1 = min(h_img, int(cy + h / 2 + pad_y))
    k = int(max(ch * 1.8, 17))
    if k % 2 == 0:
        k += 1
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return
    blurred = cv2.GaussianBlur(roi, (k, k), 0)
    blurred = cv2.GaussianBlur(blurred, (k, k), 0)
    frame[y0:y1, x0:x1] = blurred


def restore_banner(frame, orig):
    hsv = cv2.cvtColor(orig, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 90, 120), (10, 255, 255)) | cv2.inRange(
        hsv, (170, 90, 120), (180, 255, 255))
    mask = cv2.dilate(mask, np.ones((25, 25), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, best_area = None, 0
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if y > BANNER_RESTORE_MIN_Y or w < 200:
            continue
        if w * h > best_area:
            best, best_area = (x, y, w, h), w * h
    if best is None:
        return
    x, y, w, h = best
    pad = 6
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(orig.shape[1], x + w + pad), min(orig.shape[0], y + h + pad)
    frame[y0:y1, x0:x1] = orig[y0:y1, x0:x1]


def main() -> None:
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=7, initializer=_init_worker) as ex:
        per_frame = list(ex.map(_detect_orig, range(FIRST, LAST + 1)))

    # ---- 阶段1: 原始帧聚类,糊掉所有非 UI 文字 ----
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
          f"{len(skipped)} UI skipped", flush=True)

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

    # ---- 阶段2: 迭代扫描打码结果,非 UI 残字即补 ----
    for round_no in range(1, 4):
        leftovers = []
        with ProcessPoolExecutor(max_workers=7, initializer=_init_worker) as ex:
            for fidx, found in ex.map(_scan_masked, range(FIRST, LAST + 1)):
                for cx, cy, w, h, t in found:
                    if not is_ui_text(t):
                        leftovers.append((fidx, cx, cy, w, h, t))
        print(f"verify round {round_no}: {len(leftovers)} leftovers", flush=True)
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

    # ---- 阶段3: 恢复横幅 ----
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
