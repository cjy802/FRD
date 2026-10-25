# -*- coding: utf-8 -*-
"""v2 段三路验证:常规 OCR + 旋转竖排检测 + 底边带检测,报告一切可读残字。"""
import sys
import time
from pathlib import Path

import cv2
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame  # noqa: E402

FRAMES = Path(r"D:\低空经济\视频\_inspect\v2seg_frames")
FIRST, LAST = 5105, 5311
CROP = (200, 100, 1830, 1030)
SCALE = 1.6
OK = {"航点", "兴趣点", "下一步", "N/A", "飞机未连接上遥控器",
      "3.飞行路线导入", "高德地图"}

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


def ocr_img(img):
    res, _ = _ocr(img)
    out = []
    for box, text, score in (res or []):
        t = str(text).strip()
        if float(score) < 0.3 or not t:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((t, (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
    return out


def check(fidx: int):
    frame = read_frame(FRAMES / f"f_{fidx:05d}.jpg")
    if frame is None:
        return fidx, []
    bad = []
    x0c, y0c, x1c, y1c = CROP
    band = frame[y0c:y1c, x0c:x1c]
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(2.5, (8, 8)).apply(gray)
    big = cv2.resize(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), None, fx=SCALE, fy=SCALE)
    for t, cx, cy in ocr_img(big):
        if t in OK or "耗时" in t or "距离" in t or "的图" in t or "高德" in t:
            continue
        bad.append((t, "main", cx / SCALE + x0c, cy / SCALE + y0c))
    rot = cv2.rotate(band, cv2.ROTATE_90_CLOCKWISE)
    rgray = cv2.createCLAHE(2.5, (8, 8)).apply(cv2.cvtColor(rot, cv2.COLOR_BGR2GRAY))
    rbig = cv2.resize(cv2.cvtColor(rgray, cv2.COLOR_GRAY2BGR), None, fx=SCALE, fy=SCALE)
    for t, cx, cy in ocr_img(rbig):
        if t in OK or len(t) < 2 or "耗时" in t:
            continue
        H = band.shape[0]
        ox = x0c + (H - cy / SCALE)
        oy = y0c + cx / SCALE
        if 440 < ox < 620 and 540 < oy < 760:
            continue  # 已糊的大泽路区域
        bad.append((t, "rotated", ox, oy))
    strip = frame[975:1008, 240:1770]
    for t, cx, cy in ocr_img(strip):
        if t in OK:
            continue
        bad.append((t, "bottom", cx + 240, cy + 975))
    return fidx, bad


def main() -> None:
    _init()
    t0 = time.time()
    total = 0
    with ProcessPoolExecutor(max_workers=7, initializer=_init) as ex:
        for fidx, bad in ex.map(check, range(FIRST, LAST + 1)):
            for t, kind, x, y in bad:
                print(f"  frame {fidx}: [{kind}] '{t}' @ ({int(x)},{int(y)})")
                total += 1
    print(f"verify done: {total} suspicious reads, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
