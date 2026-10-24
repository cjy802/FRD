"""Rain-domain seed labels for the ponding champion (2026-10-24, 9 images).

Same shape as seed_local_labels: normalized hand-checked polygons -> 0/1 masks
-> check overlays under data/rain_seed. Sources: 6 frames picked from the two
rain videos in 测试视频/下雨 (histogram-deduped 1fps extraction, then visual
triage via contact sheets) plus the 3 standalone rain photos. Polygons were
traced per-image by a vision pass and verified once more on the check collage;
frames whose polygons failed verification were dropped rather than shipped.
These are seed labels, not ground truth: verify checks/ before training.

After review, ship to the rented 4090 and rebuild the champion with
data/mix5 = mix4 corpus + rain_seed oversampled (see data/rain_seed/TRAIN_4090.md).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "rain_raw"
PHOTOS = ROOT / ".." / "测试视频" / "下雨"
OUT = ROOT / "data" / "rain_seed"
OVERSAMPLE = 30  # copies per image (x2 with the horizontal flip), matches local_seed

SOURCES = {
    "rain_concat_001": RAW / "concat_001.jpg",
    "rain_concat_011": RAW / "concat_011.jpg",
    "rain_concat_016": RAW / "concat_016.jpg",
    "rain_concat_029": RAW / "concat_029.jpg",
    "rain_snow_001": RAW / "snow_001.jpg",
    "rain_snow_014": RAW / "snow_014.jpg",
    "rain_photo_street": PHOTOS / "24c0bd9c4871ca86f0d080e864fb4e7b.png",
    "rain_photo_campus": PHOTOS / "427790f159e52e610bb73035ceac9c50.png",
    "rain_photo_avenue": PHOTOS / "b2955f31d2961480502f6e28198cab03.png",
}

# Normalized [0..1] polygons (clockwise, origin top-left), water-only.
POLYGONS: dict[str, list[list[tuple[float, float]]]] = {
    "rain_concat_001": [
        [(0.10, 0.55), (0.28, 0.52), (0.44, 0.54), (0.52, 0.62), (0.44, 0.74), (0.28, 0.78), (0.14, 0.70)],
        [(0.62, 0.72), (0.76, 0.70), (0.82, 0.78), (0.74, 0.86), (0.64, 0.82)],
    ],
    "rain_concat_011": [
        [(0.22, 0.52), (0.35, 0.50), (0.45, 0.53), (0.50, 0.62), (0.42, 0.72), (0.30, 0.74), (0.22, 0.66)],
        [(0.60, 0.55), (0.70, 0.53), (0.74, 0.60), (0.68, 0.67), (0.60, 0.64)],
        [(0.06, 0.78), (0.18, 0.76), (0.20, 0.86), (0.10, 0.88)],
    ],
    "rain_concat_016": [
        [(0.30, 0.48), (0.46, 0.47), (0.55, 0.55), (0.48, 0.66), (0.34, 0.64)],
        [(0.08, 0.68), (0.20, 0.66), (0.24, 0.76), (0.12, 0.80)],
    ],
    "rain_concat_029": [
        [(0.18, 0.58), (0.38, 0.55), (0.58, 0.58), (0.66, 0.68), (0.52, 0.80), (0.30, 0.82), (0.16, 0.72)],
        [(0.70, 0.60), (0.82, 0.58), (0.86, 0.68), (0.76, 0.74)],
    ],
    "rain_snow_001": [
        [(0.15, 0.55), (0.35, 0.52), (0.55, 0.54), (0.62, 0.64), (0.50, 0.76), (0.30, 0.80), (0.16, 0.70)],
        [(0.68, 0.70), (0.82, 0.68), (0.88, 0.78), (0.76, 0.84)],
    ],
    "rain_snow_014": [
        [(0.12, 0.56), (0.30, 0.53), (0.42, 0.58), (0.38, 0.70), (0.24, 0.74), (0.10, 0.66)],
        [(0.66, 0.72), (0.80, 0.70), (0.86, 0.80), (0.72, 0.86)],
    ],
    "rain_photo_street": [
        [(0.05, 0.48), (0.25, 0.45), (0.42, 0.46), (0.55, 0.50), (0.58, 0.62), (0.45, 0.75), (0.28, 0.78), (0.12, 0.72), (0.05, 0.60)],
        [(0.68, 0.70), (0.82, 0.68), (0.88, 0.75), (0.82, 0.84), (0.70, 0.82)],
    ],
    "rain_photo_campus": [
        [(0.38, 0.30), (0.52, 0.28), (0.60, 0.35), (0.58, 0.48), (0.46, 0.52), (0.38, 0.46)],
        [(0.05, 0.55), (0.22, 0.52), (0.26, 0.60), (0.20, 0.68), (0.06, 0.64)],
    ],
    "rain_photo_avenue": [
        [(0.08, 0.55), (0.28, 0.52), (0.38, 0.60), (0.32, 0.75), (0.18, 0.80), (0.06, 0.70)],
        [(0.55, 0.66), (0.72, 0.64), (0.78, 0.72), (0.70, 0.80), (0.56, 0.76)],
        [(0.76, 0.40), (0.90, 0.38), (0.92, 0.48), (0.80, 0.50)],
    ],
}


def _read(path: Path) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f"无法读取图片：{path}")
    return image


def _write(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(path.suffix, image)[1].tofile(str(path))


def build() -> None:
    for folder in ("images", "masks", "checks"):
        shutil.rmtree(OUT / folder, ignore_errors=True)
        (OUT / folder).mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, source in SOURCES.items():
        image = _read(source)
        height, width = image.shape[:2]
        polygons = POLYGONS.get(name, [])
        if not polygons:
            continue
        mask = np.zeros((height, width), dtype=np.uint8)
        overlay = image.copy()
        for polygon in polygons:
            points = np.round(np.asarray(polygon) * [width - 1, height - 1]).astype(np.int32)
            cv2.fillPoly(mask, [points], 255)
            cv2.polylines(overlay, [points], True, (0, 0, 255), 3)
        blend = cv2.addWeighted(image, 0.55, overlay, 0.45, 0)
        _write(OUT / "images" / f"{name}.png", image)
        _write(OUT / "masks" / f"{name}.png", mask)
        _write(OUT / "checks" / f"{name}.png", blend)
        water = float((mask > 0).mean())
        manifest.append({"name": name, "source": str(source), "polygons": len(polygons), "water_fraction": round(water, 4)})
    (OUT / "manifest.json").write_text(
        "\n".join(__import__("json").dumps(entry, ensure_ascii=False) for entry in manifest) + "\n",
        encoding="utf-8")
    print(f"rain_seed: {len(manifest)} images -> {OUT}")
    for entry in manifest:
        print(f"  {entry['name']}: {entry['polygons']} polys, water {entry['water_fraction']:.1%}")


if __name__ == "__main__":
    build()
