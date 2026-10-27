# -*- coding: utf-8 -*-
"""扫描段视频(已完成打码)中的残留可读文字,输出清单与建议定点框。"""
import json
import subprocess
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).parent))
from mask_v6_segment import _init_worker, is_ui_text  # noqa: E402

SEG = Path(r"D:\低空经济\视频\完成_10.27_6.0_段156-169打码.mp4")
OUT = Path(r"D:\低空经济\output\v6seg_video_leftovers.json")
STEP = 10          # 每 10 帧抽检
SCALE = 2.0

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
        out.append((t, (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2,
                    max(xs) - min(xs), max(ys) - min(ys)))
    return out


def main() -> None:
    _init()
    tmp = Path(r"D:\低空经济\视频\_inspect\v6video_scan")
    tmp.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(SEG),
                    "-vf", f"select=not(mod(n\\,{STEP}))", "-vsync", "0",
                    "-q:v", "2", str(tmp / "s_%05d.jpg")], check=True)

    findings = []
    files = sorted(tmp.glob("s_*.jpg"))
    for i, p in enumerate(files):
        seg_frame = i * STEP          # 段内帧号(0-based)
        video_fidx = 4699 + seg_frame  # 对应 6.0 帧号
        img = cv2.imread(str(p))
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.createCLAHE(2.5, (8, 8)).apply(gray)
        big = cv2.resize(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), None,
                         fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
        for t, cx, cy, w, h in ocr_img(big):
            cx, cy, w, h = cx / SCALE, cy / SCALE, w / SCALE, h / SCALE
            if cy > 990:      # 字幕
                continue
            if is_ui_text(t):
                continue
            if t in {"飞机未连接上遥控器", "N/A", "航点", "兴趣点", "25m", "下一步",
                     "历史任务", "开启航点飞行", "高德地图", "42m", "45m", "40m"}:
                continue
            if "耗时" in t or "距离" in t or "2026" in t or "DJI" in t.upper():
                continue
            findings.append({"video_frame": video_fidx, "seg_frame": seg_frame,
                             "text": t, "x": int(cx), "y": int(cy),
                             "w": int(w), "h": int(h)})
    OUT.write_text(json.dumps(findings, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(findings)} leftover reads -> {OUT}")
    for f in findings[:40]:
        print(f"  v6frame {f['video_frame']}: '{f['text']}' @ ({f['x']},{f['y']}) {f['w']}x{f['h']}")


if __name__ == "__main__":
    main()
