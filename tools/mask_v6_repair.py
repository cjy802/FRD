# -*- coding: utf-8 -*-
"""诊断 v6seg_frames 中未处理的帧(与原始帧几乎无差异),并对它们带重试补打码。"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import read_frame, write_frame  # noqa: E402
from mask_v6_segment import (FRAMES_DIR, ORIG_DIR, FIRST, LAST,  # noqa: E402
                             _init_worker, detect, is_ui_text, blur_box, restore_banner)

UNPROCESSED_DIFF = 2.0   # 平均差分低于此值视为"未被处理"


def main() -> None:
    global _ocr
    _init_worker()
    t0 = time.time()

    failed = []
    for fidx in range(FIRST, LAST + 1):
        got = False
        for attempt in range(6):
            orig = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
            masked = read_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg")
            if orig is None or masked is None:
                time.sleep(1.0)
                continue
            got = True
            diff = float(np.abs(cv2.cvtColor(orig, cv2.COLOR_BGR2GRAY).astype(np.int16)
                                - cv2.cvtColor(masked, cv2.COLOR_BGR2GRAY).astype(np.int16)).mean())
            if diff < UNPROCESSED_DIFF:
                failed.append(fidx)
            break
        if not got:
            failed.append(fidx)
    print(f"unprocessed/unreadable frames: {len(failed)} -> {failed[:40]}", flush=True)

    # 对失败帧:重试读取原始帧,重新打码(检测非UI文字->像素化),带指数退避
    done = 0
    still_failed = []
    for fidx in failed:
        ok = False
        for attempt in range(8):
            orig = read_frame(ORIG_DIR / f"f_{fidx:05d}.jpg")
            if orig is None:
                time.sleep(0.5 * (attempt + 1))
                continue
            found = detect(orig)
            texts = [t for _, _, _, _, t in found if not is_ui_text(t)]
            masked = orig.copy()
            for cx, cy, w, h, t in found:
                if is_ui_text(t):
                    continue
                blur_box(masked, cx, cy, w, h, t)
            ok = True
            # 残留复核(重试读取写回目标)
            for attempt2 in range(8):
                try:
                    write_frame(FRAMES_DIR / f"f_{fidx:05d}.jpg", masked)
                    done += 1
                    break
                except Exception:
                    time.sleep(0.5 * (attempt2 + 1))
            break
        if not ok:
            still_failed.append(fidx)
    print(f"re-masked {done} frames, still failed: {still_failed}", flush=True)
    print(f"total time {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
