# -*- coding: utf-8 -*-
"""v2 段定点补漏:扫描发现的地名残字逐一定位模糊。"""
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402

FRAMES = Path(r"D:\低空经济\视频\_inspect\v2seg_frames")

# (起始帧, 结束帧, 中心x, 中心y, 半宽, 半高)
PATCHES = [
    (5203, 5252, 692, 231, 65, 24),   # 电子阅览室
    (5262, 5281, 797, 246, 68, 24),   # 职业技术大学残字
    (5257, 5281, 350, 706, 58, 30),   # 学生会
    (5204, 5209, 961, 703, 70, 22),   # 社团活动广场
    (5124, 5134, 1346, 927, 62, 22),  # 电子阅览室(右下)
]
# 单帧: (帧号, 中心x, 中心y) —— 音乐喷泉残字
MUSIC = [(5181, 1106, 312), (5182, 1086, 277), (5187, 1003, 177),
         (5188, 1002, 176), (5189, 1034, 167), (5191, 1031, 166),
         (5196, 1000, 171)]


def blur(frame, cx, cy, hw, hh):
    x0, y0 = max(0, cx - hw), max(0, cy - hh)
    x1, y1 = cx + hw, cy + hh
    k = 41
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return
    b = cv2.GaussianBlur(roi, (k, k), 0)
    b = cv2.GaussianBlur(b, (k, k), 0)
    frame[y0:y1, x0:x1] = b


def main() -> None:
    targets = {}
    for a, b_, cx, cy, hw, hh in PATCHES:
        for f in range(a, b_ + 1):
            targets.setdefault(f, []).append((cx, cy, hw, hh))
    for f, cx, cy in MUSIC:
        targets.setdefault(f, []).append((cx, cy, 52, 18))

    for f in sorted(targets):
        p = FRAMES / f"f_{f:05d}.jpg"
        frame = read_frame(p)
        if frame is None:
            continue
        for cx, cy, hw, hh in targets[f]:
            blur(frame, cx, cy, hw, hh)
        write_frame(p, frame)
    print(f"patched {len(targets)} frames")


if __name__ == "__main__":
    main()
