# -*- coding: utf-8 -*-
"""对 2.0 演示视频 170.2-177.0s(DJI RC2 实拍段)的抽帧做 OCR 地名打码。

用法: python tools/mask_v2_segment.py
抽帧目录: 视频/_inspect/v2seg_frames(帧号 5105-5311)
"""
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mask_map_labels import (  # noqa: E402
    blur_region, char_height, read_frame, should_mask, write_frame,
)

FRAMES_DIR = Path(r"D:\低空经济\视频\_inspect\v2seg_frames")
FIRST, LAST = 5105, 5311

_ocr = None


def _init_worker():
    global _ocr
    import rapidocr_onnxruntime.utils as rutils

    _orig_options = rutils.SessionOptions

    def _limited_options():
        opt = _orig_options()
        opt.intra_op_num_threads = 4
        opt.inter_op_num_threads = 1
        return opt

    rutils.SessionOptions = _limited_options
    from rapidocr_onnxruntime import RapidOCR

    _ocr = RapidOCR(det_model_path=None, cls_model_path=None, rec_model_path=None)


def _process_frame(fidx: int):
    path = FRAMES_DIR / f"f_{fidx:05d}.jpg"
    frame = read_frame(path)
    if frame is None:
        return fidx, 0, False
    res, _ = _ocr(frame)
    hits = 0
    changed = False
    if res:
        h_img, w_img = frame.shape[:2]
        for box, text, score in res:
            ch, x, y, w, h = char_height(box)
            if not should_mask(text.strip(), float(score), ch, y):
                continue
            pad = max(2, int(ch * 0.4))
            x0 = max(0, x - pad)
            y0 = max(0, y - pad)
            x1 = min(w_img, x + w + pad)
            y1 = min(h_img, y + h + pad)
            blur_region(frame, x0, y0, x1, y1, ch)
            hits += 1
            changed = True
    if changed:
        write_frame(path, frame)
    return fidx, hits, changed


def main() -> None:
    t0 = time.time()
    total_hits = touched = 0
    with ProcessPoolExecutor(max_workers=7, initializer=_init_worker) as ex:
        for i, (fidx, hits, changed) in enumerate(
            ex.map(_process_frame, range(FIRST, LAST + 1)), 1
        ):
            total_hits += hits
            touched += int(changed)
            if i % 50 == 0:
                print(f"  {i}/{LAST - FIRST + 1} frames, hits={total_hits}, "
                      f"touched={touched}, {time.time()-t0:.0f}s", flush=True)
    print(f"done: {total_hits} regions blurred on {touched} frames, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
