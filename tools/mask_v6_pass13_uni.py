# -*- coding: utf-8 -*-
"""6.0 段校名残字专项:检出"大学/学院/职业技术"类文本的框大幅右扩像素化,
并生成帧级扩框清单(boxes3)供合成脚本叠加时域聚合框。"""
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame  # noqa: E402
from mask_v6_segment import FIRST, LAST, _init_worker, is_ui_text  # noqa: E402

ORIG_DIR = Path(r"D:\低空经济\视频\_inspect\v6seg_orig")
FRAMES_DIR = Path(r"D:\低空经济\视频\_inspect\v6seg_frames")
OUT_JSON = Path(r"D:\低空经济\output\v6seg_uni_boxes.json")
SCALE = 1.6

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


def detect_uni(fidx: int):
    frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
    if frame is None:
        for _ in range(5):
            time.sleep(0.8)
            frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
            if frame is not None:
                break
    if frame is None:
        return fidx, []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    big = cv2.resize(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), None,
                     fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
    res, _ = _ocr(big)
    out = []
    for box, text, score in (res or []):
        t = str(text).strip()
        if float(score) < 0.15 or not t:
            continue
        # 校名类:含大学/学院/职业技术/业技 变体,或被遮挡只读出半截的
        if not any(w in t for w in ("大学", "学院", "学校", "职业技术", "业技", "技术")):
            continue
        if is_ui_text(t):
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        w = (max(xs) - min(xs)) / SCALE
        h = (max(ys) - min(ys)) / SCALE
        if h > 50:
            continue
        cx = (min(xs) + max(xs)) / 2 / SCALE
        cy = (min(ys) + max(ys)) / 2 / SCALE
        out.append((cx, cy, w, h, t))
    return fidx, out


def main() -> None:
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=7, initializer=_init) as ex:
        per_frame = dict(ex.map(detect_uni, range(FIRST, LAST + 1)))
    out = {f: v for f, v in per_frame.items() if v}
    n = sum(len(v) for v in out.values())
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"uni boxes: {n} on {len(out)} frames -> {OUT_JSON}, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
