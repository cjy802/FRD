# -*- coding: utf-8 -*-
"""6.0 段横幅兜底:精确检测失败的帧改用邻近帧的横幅框从原始帧恢复。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402
from mask_v6_fix import banner_box_precise  # noqa: E402
from mask_v6_segment import FRAMES_DIR, ORIG_DIR, FIRST, LAST  # noqa: E402


def main() -> None:
    t0 = time.time()
    # 第一遍:收集每帧的横幅框
    boxes = {}
    no_box = []
    for fidx in range(FIRST, LAST + 1):
        orig = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
        if orig is None:
            continue
        box = banner_box_precise(orig)
        if box:
            boxes[fidx] = box
        else:
            no_box.append(fidx)
    known = sorted(boxes)
    print(f"banner box: {len(known)} frames, {len(no_box)} need neighbor fallback", flush=True)

    # 第二遍:无框帧用最近有框帧的位置恢复
    restored = 0
    for fidx in range(FIRST, LAST + 1):
        orig = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
        masked = read_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg")
        if orig is None or masked is None:
            continue
        if fidx in boxes:
            box = boxes[fidx]
        else:
            near = min(known, key=lambda k: abs(k - fidx)) if known else None
            if near is None or abs(near - fidx) > 90:
                continue
            box = boxes[near]
        x, y, w, h = box
        x0, y0 = max(0, x - 6), max(0, y - 6)
        x1, y1 = min(orig.shape[1], x + w + 6), min(orig.shape[0], y + h + 6)
        masked[y0:y1, x0:x1] = orig[y0:y1, x0:x1]
        write_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg", masked)
        restored += 1
    print(f"pass12 done: banner restored on {restored} frames, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
