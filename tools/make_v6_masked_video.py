# -*- coding: utf-8 -*-
"""6.0 段打码直出视频:检测在原始帧上完成,像素化在内存完成,直接 pipe 给
ffmpeg 编码,全程不写 jpg 到 _inspect(该目录存在文件回滚现象)。

用法: python tools/make_v6_masked_video.py
输出: 视频/完成_10.27_6.0_段156-169打码.mp4 (无音频,接入时用总片原声)
"""
import json
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame  # noqa: E402
from mask_v2_pass2 import _strong_place  # noqa: E402
from mask_v6_segment import FIRST, LAST, _init_worker, is_ui_text  # noqa: E402

ORIG_DIR = Path(r"D:\低空经济\视频\_inspect\v6seg_orig")
OUT_VIDEO = Path(r"D:\低空经济\视频\完成_10.27_6.0_段156-169打码.mp4")
BOXES_JSON = Path(r"D:\低空经济\output\v6seg_blur_boxes2.json")
FPS = 30.0
W, H = 1916, 1080


def detect_only(fidx: int):
    from mask_v6_segment import detect
    frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
    if frame is None:
        for _ in range(6):
            time.sleep(0.8)
            frame = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
            if frame is not None:
                break
    if frame is None:
        return fidx, None
    return fidx, detect(frame)


def build_boxes() -> dict:
    """原始帧全量文字聚类;非 UI 类 → 每帧糊框列表。"""
    with ProcessPoolExecutor(max_workers=7, initializer=_init_worker) as ex:
        per_frame = list(ex.map(detect_only, range(FIRST, LAST + 1)))
    clusters: list[dict] = []
    for fidx, found in per_frame:
        if found is None:
            continue
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
    per_frame_boxes: dict[int, list] = {}
    for c in targets:
        for fidx, (cx, cy, w, h, t) in c["frames"].items():
            per_frame_boxes.setdefault(fidx, []).append((cx, cy, w, h, t))
    return per_frame_boxes


def pixelate(frame, cx, cy, w, h, t: str):
    ch = max(9.0, h)
    pad_x = max(14, int(w * 0.9 if any(x in t for x in ("大学", "学院", "学校", "职业技术")) else w * 0.45))
    extra_right = int(w * 0.7) if t.endswith("_uni") else 0
    pad_y = max(5, int(h * 0.55))
    x0 = max(0, int(cx - w / 2 - pad_x))
    y0 = max(0, int(cy - h / 2 - pad_y))
    x1 = min(W, int(cx + w / 2 + pad_x + extra_right))
    y1 = min(H, int(cy + h / 2 + pad_y))
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return
    block = 12
    small = cv2.resize(roi, (max(2, (x1 - x0) // block), max(2, (y1 - y0) // block)),
                       interpolation=cv2.INTER_AREA)
    mosaic = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    mosaic = cv2.GaussianBlur(mosaic, (7, 7), 0)
    frame[y0:y1, x0:x1] = mosaic


def main() -> None:
    t0 = time.time()
    if BOXES_JSON.exists():
        per_frame_boxes = {int(k): v for k, v in
                           json.loads(BOXES_JSON.read_text(encoding="utf-8")).items()}
        print(f"boxes loaded from cache: {sum(len(v) for v in per_frame_boxes.values())}", flush=True)
    else:
        per_frame_boxes = build_boxes()
        BOXES_JSON.parent.mkdir(exist_ok=True)
        BOXES_JSON.write_text(json.dumps({k: v for k, v in per_frame_boxes.items()},
                                         ensure_ascii=False), encoding="utf-8")
        print(f"boxes built: {sum(len(v) for v in per_frame_boxes.values())}, "
              f"{time.time()-t0:.0f}s", flush=True)

    # 已知绿线沿路路名牌/竖排路名的固定 overlay(OCR 对绿底白字检出率低)
    OVERLAY_FIXED = [
        (4699, 5076, 580, 372, 725, 415),   # 创业大道 路牌(绿线上)
        (4699, 5076, 636, 408, 692, 555),   # 信息大道 竖排
        (4699, 5076, 345, 545, 480, 595),   # 9号楼
        (4699, 5076, 455, 550, 600, 750),   # 大泽路 竖排(左缘)
        # 手柄大地图段(暗屏/地图画面)的固定标签位
        (4909, 5076, 575, 360, 735, 415),   # 创业大道
        (4909, 5076, 395, 468, 510, 522),   # 天泽楼
        (4909, 5076, 335, 543, 488, 598),   # 9号楼
        (4909, 5076, 830, 438, 980, 552),   # 软件与大数据学院(校名)
        (4909, 5076, 850, 273, 978, 338),   # 行政楼
        (4909, 5076, 915, 603, 1080, 652),  # 工业中心B区
        (4909, 5076, 1175, 213, 1280, 268), # 音乐喷泉
    ]
    for a, b, x0, y0, x1, y1 in OVERLAY_FIXED:
        for f in range(max(FIRST, a), min(LAST, b) + 1):
            per_frame_boxes.setdefault(f, []).append(
                ((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0, "fixed_overlay"))

    # 校名专项框(原始帧检出,已含大幅右扩)
    uni_json = Path(r"D:\低空经济\output\v6seg_uni_boxes.json")
    if uni_json.exists():
        uni = json.loads(uni_json.read_text(encoding="utf-8"))
        for f, boxes in uni.items():
            per_frame_boxes.setdefault(int(f), []).extend(
                (cx, cy, w, h, t + "_uni") for cx, cy, w, h, t in boxes)

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

    n_frames = int((LAST - FIRST + 1))
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
            print(f"  {i+1}/{n_frames} frames piped, {time.time()-t0:.0f}s", flush=True)
    ff.stdin.close()
    ff.wait()
    print(f"done: {OUT_VIDEO}, rc={ff.returncode}, {time.time()-t0:.0f}s total")


if __name__ == "__main__":
    main()
