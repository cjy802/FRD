# -*- coding: utf-8 -*-
"""v2 段 pass11:顽固残字与楼名像素化打码。"""
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402

FRAMES = Path(r"D:\低空经济\视频\_inspect\v2seg_frames")

JOBS = [
    (5203, 5252, 620, 205, 765, 258, "电子阅览室"),
    (5260, 5281, 725, 218, 870, 272, "职业技术大学残字"),
    (5140, 5311, 1380, 240, 1500, 295, "E楼A"),
    (5140, 5311, 1505, 312, 1625, 368, "E楼B"),
]


def pixelate(frame, x0, y0, x1, y1):
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0 or x1 - x0 < 4 or y1 - y0 < 4:
        return
    sw = max(2, (x1 - x0) // 14)
    sh = max(2, (y1 - y0) // 14)
    small = cv2.resize(roi, (sw, sh), interpolation=cv2.INTER_AREA)
    mosaic = cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    mosaic = cv2.GaussianBlur(mosaic, (7, 7), 0)
    frame[y0:y1, x0:x1] = mosaic


def main() -> None:
    count = 0
    for a, b, x0, y0, x1, y1, tag in JOBS:
        for f in range(a, b + 1):
            p = FRAMES / f"f_{f:05d}.jpg"
            frame = read_frame(p)
            if frame is None:
                continue
            pixelate(frame, x0, y0, x1, y1)
            write_frame(p, frame)
            count += 1
        print(f"  {tag}: frames {a}-{b} done", flush=True)
    print(f"pass11 pixelated {count} frame-patches")


if __name__ == "__main__":
    main()
