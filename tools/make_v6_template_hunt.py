# -*- coding: utf-8 -*-
"""6.0 段大学名标签模板追捕 + 全框源合并 + 内存直出段视频。

阶段1: 多进程对每帧做多尺度 matchTemplate 追捕"常州信息职业技术大学"
      标签(含被遮挡残字形态),命中位置记录。
阶段2: 合并全部框源(时域聚合+固定overlay+校名专项+模板命中),
      逐帧像素化后直接 pipe 给 ffmpeg,输出段视频(不落地 jpg)。
"""
import json
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402
from mask_v6_segment import FIRST, LAST, _init_worker, is_ui_text  # noqa: E402

ORIG_DIR = Path(r"D:\低空经济\视频\_inspect\v6seg_orig")
FRAMES_DIR = Path(r"D:\低空经济\视频\_inspect\v6seg_frames")
TEMPLATE = Path(r"D:\低空经济\视频\_inspect\v6seg_uni_template.jpg")
OUT_VIDEO = Path(r"D:\低空经济\视频\完成_10.27_6.0_段156-169打码.mp4")
BOXES2 = Path(r"D:\低空经济\output\v6seg_blur_boxes2.json")
UNI_JSON = Path(r"D:\低空经济\output\v6seg_uni_boxes.json")
FPS = 30.0
W, H = 1916, 1080
SCALES = [1.0]
THRESH = 0.5

_tpl_gray = None


def _hunt_init():
    _init_worker()
    global _tpl_gray
    src = read_frame(ORIG_DIR / "f_04960.jpg")
    _tpl_gray = cv2.cvtColor(src[365:400, 730:940], cv2.COLOR_BGR2GRAY)


def hunt(fidx: int):
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
    hits = []
    for s in SCALES:
        tw = int(_tpl_gray.shape[1] * s)
        th = int(_tpl_gray.shape[0] * s)
        if tw < 40 or tw >= gray.shape[1]:
            continue
        tpl = cv2.resize(_tpl_gray, (tw, th), interpolation=cv2.INTER_AREA)
        r = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(r >= THRESH)
        for x, y in zip(xs, ys):
            # 命中框:模板区域 + 上方一行("常州信息职")+ 右侧余量
            hits.append((int(x) - 15, int(y) - th - 12, int(tw) + 90, int(th) * 2 + 30))
    return fidx, hits


def pixelate(frame, cx, cy, w, h, t: str = ""):
    ch = max(9.0, h)
    pad_x = max(14, int(w * 0.9 if any(x in t for x in ("大学", "学院", "学校", "职业技术")) else w * 0.45))
    pad_y = max(5, int(h * 0.55))
    x0 = max(0, int(cx - w / 2 - pad_x))
    y0 = max(0, int(cy - h / 2 - pad_y))
    x1 = min(W, int(cx + w / 2 + pad_x))
    y1 = min(H, int(cy + h / 2 + pad_y))
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return
    small = cv2.resize(roi, (max(2, (x1 - x0) // 12), max(2, (y1 - y0) // 12)),
                       interpolation=cv2.INTER_AREA)
    mosaic = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    mosaic = cv2.GaussianBlur(mosaic, (7, 7), 0)
    frame[y0:y1, x0:x1] = mosaic


def merge_boxes(per_frame_boxes: dict) -> dict:
    merged: dict[int, list] = {}
    def add(fidx, box):
        merged.setdefault(fidx, []).append(box)
    # 时域聚合框
    b2 = json.loads(BOXES2.read_text(encoding="utf-8"))
    for k, boxes in b2.items():
        for cx, cy, w, h, t in boxes:
            add(int(k), (cx, cy, w, h, t))
    # 校名专项框(右扩在 pixelate 内由 _uni 后缀处理)
    if UNI_JSON.exists():
        uni = json.loads(UNI_JSON.read_text(encoding="utf-8"))
        for k, boxes in uni.items():
            for cx, cy, w, h, t in boxes:
                add(int(k), (cx, cy, w, h, t + "_uni"))
    # 模板命中框
    hits_all = json.loads(Path(r"D:\低空经济\output\v6seg_template_hits.json").read_text(encoding="utf-8"))
    for k, boxes in hits_all.items():
        for cx, cy, w, h in boxes:
            add(int(k), (cx, cy, w, h, "template"))
    # 固定 overlay(绿线路牌/竖排路名/楼名等,OCR 检出率低的位置)
    OVERLAY_FIXED = [
        (4699, 5076, 580, 372, 725, 415),   # 创业大道 路牌
        (4699, 5076, 636, 408, 692, 555),   # 信息大道 竖排
        (4699, 5076, 345, 545, 480, 595),   # 9号楼
        (4699, 5076, 455, 550, 600, 750),   # 大泽路 竖排
        (4909, 5076, 575, 360, 735, 415),   # 创业大道
        (4909, 5076, 395, 468, 510, 522),   # 天泽楼
        (4909, 5076, 335, 543, 488, 598),   # 9号楼
        (4909, 5076, 830, 438, 980, 552),   # 软件与大数据学院
        (4909, 5076, 850, 273, 978, 338),   # 行政楼
        (4909, 5076, 915, 603, 1080, 652),  # 工业中心B区
        (4909, 5076, 1175, 213, 1280, 268), # 音乐喷泉
        (4909, 5076, 548, 540, 615, 708),   # 文津路 竖排
        (4909, 5076, 730, 638, 855, 688),   # 文萃路
        (4699, 5076, 585, 240, 705, 278),   # 电子阅览室(横幅下)
    ]
    for a, b, x0, y0, x1, y1 in OVERLAY_FIXED:
        for f in range(max(FIRST, a), min(LAST, b) + 1):
            add(f, ((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0, "fixed_overlay"))
    return merged


def main() -> None:
    t0 = time.time()
    _hunt_init()
    hits = {}
    for fidx in range(FIRST, LAST + 1):
        f, found = hunt(fidx)
        hits[f] = found
        if (fidx - FIRST) % 60 == 0:
            print(f"  hunt {fidx-FIRST}/{LAST-FIRST+1}, hits so far {sum(len(v) for v in hits.values())}", flush=True)
    total_hits = sum(len(v) for v in hits.values())
    Path(r"D:\低空经济\output\v6seg_template_hits.json").write_text(
        json.dumps({str(k): v for k, v in hits.items()}), encoding="utf-8")
    frames_with_hits = sum(1 for v in hits.values() if v)
    print(f"template hunt: {total_hits} hits on {frames_with_hits} frames, "
          f"{time.time()-t0:.0f}s", flush=True)

    per_frame_boxes = merge_boxes(hits)

    ff = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(int(FPS)),
         "-i", "-",
         "-ss", "156.6333", "-t", "12.5667",
         "-i", str(Path(r"D:\低空经济\视频\完成_10.27_6.0.mp4")),
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-crf", "18", "-preset", "fast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", "-shortest",
         str(OUT_VIDEO)],
        stdin=subprocess.PIPE)

    n_frames = LAST - FIRST + 1
    for i in range(n_frames):
        fidx = FIRST + i
        frame = None
        for attempt in range(8):
            frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
            if frame is not None:
                break
            time.sleep(0.6 * (attempt + 1))
        if frame is None:
            frame = np.zeros((H, W, 3), dtype=np.uint8)
        if frame.shape[1] != W:
            frame = cv2.resize(frame, (W, H))
        for cx, cy, w, h, t in per_frame_boxes.get(fidx, []):
            pixelate(frame, cx, cy, w, h, t)
        ff.stdin.write(frame.tobytes())
        if (i + 1) % 60 == 0:
            print(f"  {i+1}/{n_frames} piped, {time.time()-t0:.0f}s", flush=True)
    ff.stdin.close()
    ff.wait()
    print(f"done: {OUT_VIDEO}, rc={ff.returncode}, {time.time()-t0:.0f}s total")


if __name__ == "__main__":
    main()
