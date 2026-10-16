"""One-off seed annotation for the 3 user photos; polygons hand-drawn after visual review.

Creates 0/1 masks, renders check overlays, and builds a mixed fine-tune dataset:
FRED train pairs + local photos augmented (horizontal flip) and oversampled so the
rare local "murky pothole" domain gets meaningful gradient. Verify overlays before
training; these are seed labels, not ground truth.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(".")
LOCAL_SRC = {
    "real_potholes": "demo/outbox/20261016_181004/inputs/real_potholes.png",
    "real_flooded_street": "demo/outbox/20261016_181004/inputs/real_flooded_street.png",
    "real_wide_road": "demo/outbox/20261016_210034/inputs/real_wide_road.png",
}
# Normalized [0..1] polygons traced from the displayed images.
POLYGONS = {
    "real_potholes": [
        [(0.213, 0.577), (0.288, 0.548), (0.374, 0.534), (0.470, 0.544), (0.566, 0.563), (0.638, 0.592),
         (0.662, 0.629), (0.623, 0.687), (0.537, 0.731), (0.431, 0.753), (0.316, 0.738), (0.240, 0.687),
         (0.213, 0.629)],
        [(0.767, 0.336), (0.806, 0.327), (0.834, 0.338), (0.830, 0.357), (0.792, 0.363), (0.770, 0.354)],
        [(0.532, 0.336), (0.592, 0.330), (0.623, 0.342), (0.618, 0.366), (0.573, 0.377), (0.537, 0.363)],
    ],
    "real_flooded_street": [
        [(0.042, 0.313), (0.278, 0.281), (0.556, 0.266), (0.833, 0.281), (0.958, 0.333), (0.986, 0.417),
         (0.917, 0.500), (0.833, 0.583), (0.694, 0.667), (0.528, 0.750), (0.347, 0.813), (0.167, 0.854),
         (0.083, 0.729), (0.028, 0.521)],
        [(0.000, 0.833), (0.333, 0.875), (0.667, 0.917), (1.000, 0.958), (1.000, 1.000), (0.000, 1.000)],
    ],
    "real_wide_road": [
        [(0.144, 0.531), (0.264, 0.493), (0.390, 0.477), (0.493, 0.493), (0.537, 0.538), (0.508, 0.601),
         (0.421, 0.683), (0.311, 0.723), (0.206, 0.691), (0.149, 0.608)],
        [(0.595, 0.364), (0.678, 0.349), (0.730, 0.364), (0.722, 0.410), (0.649, 0.432), (0.600, 0.402)],
    ],
}
LOCAL_COPIES = 15  # oversample factor for the 3 local photos (x2 flips)


def build_masks() -> None:
    out = ROOT / "data/local_seed"
    (out / "masks").mkdir(parents=True, exist_ok=True)
    (out / "checks").mkdir(exist_ok=True)
    for name, polygons in POLYGONS.items():
        data = np.fromfile(LOCAL_SRC[name], dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        height, width = image.shape[:2]
        (out / "images").mkdir(parents=True, exist_ok=True)
        cv2.imencode(".png", image)[1].tofile(str(out / "images" / f"{name}.png"))
        mask = np.zeros((height, width), dtype=np.uint8)
        overlay = image.copy()
        for polygon in polygons:
            points = np.array([[int(x * width), int(y * height)] for x, y in polygon], dtype=np.int32)
            cv2.fillPoly(mask, [points], 1)
            cv2.polylines(overlay, [points], True, (0, 0, 255), 2)
        blended = cv2.addWeighted(image, 0.65, cv2.merge([
            (mask * 255), np.zeros_like(mask), np.zeros_like(mask)]).astype(np.uint8), 0.35, 0)
        cv2.polylines(blended, [np.array([[int(x * width), int(y * height)] for x, y in p], np.int32) for p in polygons], True, (0, 0, 255), 2)
        cv2.imencode(".png", blended)[1].tofile(str(out / "checks" / f"{name}__check.png"))
        np.save(out / "masks" / f"{name}.npy", mask)
        fraction = mask.mean()
        print(f"{name}: {len(polygons)} 多边形, 水占比 {fraction*100:.1f}%", flush=True)


def build_mix() -> None:
    mix = ROOT / "data/local_mix"
    for sub in ("images", "masks", "val/images", "val/masks"):
        (mix / sub).mkdir(parents=True, exist_ok=True)
    fred = ROOT / "data/fred_ft"
    for folder in ("images", "masks"):
        for src in (fred / folder).iterdir():
            shutil.copy2(src, mix / folder / src.name)
    for src in (fred / "val/images").iterdir():
        shutil.copy2(src, mix / "val/images" / src.name)
        shutil.copy2(fred / "val/masks" / src.name, mix / "val/masks" / src.name)
    masks = ROOT / "data/local_seed/masks"
    images = ROOT / "data/local_seed/images"
    for mask_path in sorted(masks.glob("*.npy")):
        mask = np.load(mask_path)
        data = np.fromfile(str(images / f"{mask_path.stem}.png"), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        variants = {"": image, "_flip": cv2.flip(image, 1)}
        mask_variants = {"": mask, "_flip": cv2.flip(mask, 1)}
        for copy in range(LOCAL_COPIES):
            for suffix, img in variants.items():
                name = f"local_{mask_path.stem}{suffix}_{copy:02d}"
                cv2.imencode(".png", img)[1].tofile(str(mix / "images" / f"{name}.png"))
                cv2.imencode(".png", mask_variants[suffix])[1].tofile(str(mix / "masks" / f"{name}.png"))
    counts = {folder: len(list((mix / folder).glob("*.png"))) for folder in ("images", "masks", "val/images", "val/masks")}
    print(json.dumps({"mix": {k: v for k, v in counts.items()}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    build_masks()
    build_mix()
