# -*- coding: utf-8 -*-
"""提取演示视频底部烧录字幕,输出纯文本与 SRT。

用法: python tools/extract_subtitles.py
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

VIDEO = Path(r"D:\低空经济\视频\完整视频_10.25_1.0.mp4")
OUT_TXT = VIDEO.with_name(VIDEO.stem + "_字幕.txt")
OUT_SRT = VIDEO.with_name(VIDEO.stem + ".srt")
FPS = 30.0
BAND = (990, 1085)   # 字幕条所在的像素行范围
DIFF_THRESHOLD = 2.5  # 帧间平均差分阈值,低于它认为字幕未变化


def ocr_band(ocr, band):
    result, _ = ocr(band)
    if not result:
        return ""
    items = sorted(result, key=lambda r: min(p[0] for p in r[0]))
    parts = []
    for item in items:
        text = str(item[1]).strip()
        score = float(item[2])
        if score >= 0.5 and text:
            parts.append(text)
    return " ".join(parts)


def fmt_ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main() -> None:
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    cap = cv2.VideoCapture(str(VIDEO))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    entries: list[tuple[int, str]] = []  # (起始帧, 文本)
    current = ""
    prev_band = None
    t0 = time.time()

    for fidx in range(total):
        ok, frame = cap.read()
        if not ok:
            break
        band = frame[BAND[0]:BAND[1]]
        gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY).astype(np.int16)
        if prev_band is not None:
            diff = float(np.abs(gray - prev_band).mean())
            if diff < DIFF_THRESHOLD:
                prev_band = gray
                continue
        prev_band = gray
        text = ocr_band(ocr, band)
        if text and text != current:
            current = text
            entries.append((fidx, text))
        if fidx % 600 == 0:
            print(f"  frame {fidx}/{total}, {len(entries)} entries, {time.time()-t0:.0f}s", flush=True)

    cap.release()
    # 相邻合并:同一句被 OCR 抖动拆开时按前缀/包含关系合并
    merged: list[tuple[int, str]] = []
    for start, text in entries:
        if merged and (text in merged[-1][1] or merged[-1][1] in text):
            if len(text) > len(merged[-1][1]):
                merged[-1] = (merged[-1][0], text)
            continue
        merged.append((start, text))

    lines = []
    for i, (start, text) in enumerate(merged):
        end = merged[i + 1][0] if i + 1 < len(merged) else total
        lines.append(f"{i+1}\n{fmt_ts(start / FPS)} --> {fmt_ts(max(start + 1, end) / FPS)}\n{text}\n")
    OUT_SRT.write_text("\n".join(lines), encoding="utf-8")
    OUT_TXT.write_text("\n".join(text for _, text in merged) + "\n", encoding="utf-8")
    print(f"done: {len(merged)} subtitle lines, {time.time()-t0:.0f}s total")
    print(f"txt: {OUT_TXT}")
    print(f"srt: {OUT_SRT}")


if __name__ == "__main__":
    sys.exit(main())
