"""Pre-annotate local ponding photos as labelme polygons from the reflection cue.

Generates one labelme JSON per photo (label "water") from the classical
bright/low-saturation/smooth cue, so labellers only need to fix, add and delete
polygons in labelme instead of drawing from scratch. Also writes a preview PNG
per photo. Pre-annotations are deliberately generous (low threshold): deleting a
wrong polygon is faster than drawing a missed one.

Workflow:
    python -m waterwatch.prelabel_reflection --photos data/local_raw --out data/local_labelme
    # open data/local_labelme in labelme, fix polygons, keep label "water"
    python -m waterwatch.mask-from-labelme ... (per file, task shoreline)  -> masks/
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from .demo_watch import imread_unicode, imwrite_unicode, heuristic_reflection_mask

SUPPORTED = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
LABEL = "water"


def mask_to_polygons(mask: np.ndarray, min_area: int = 300, epsilon_ratio: float = 0.003) -> list[list[list[float]]]:
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    polygons = []
    for contour in contours:
        if cv2.contourArea(contour) < min_area:
            continue
        epsilon = epsilon_ratio * cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, epsilon, True)
        if len(approx) < 3:
            continue
        polygons.append([[round(float(point[0][0]), 1), round(float(point[0][1]), 1)] for point in approx])
    return polygons


def labelme_document(name: str, width: int, height: int, polygons: list[list[list[float]]]) -> dict:
    return {
        "version": "5.3.1",
        "flags": {},
        "shapes": [{"label": LABEL, "points": polygon, "group_id": None,
                    "shape_type": "polygon", "flags": {}} for polygon in polygons],
        "imagePath": name,
        "imageData": None,
        "imageHeight": height,
        "imageWidth": width,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--photos", type=Path, required=True, help="folder of photos to pre-annotate")
    parser.add_argument("--out", type=Path, required=True, help="output folder for labelme JSONs and previews")
    parser.add_argument("--threshold", type=float, default=0.5, help="deliberately low to over-propose regions")
    parser.add_argument("--min-pixels", type=int, default=150)
    parser.add_argument("--min-area", type=int, default=300, help="minimum polygon area in pixels")
    args = parser.parse_args()
    photos = args.photos.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "previews").mkdir(exist_ok=True)
    paths = sorted(p for p in photos.iterdir() if p.suffix.lower() in SUPPORTED)
    if not paths:
        raise SystemExit(f"{photos} 中没有可识别的照片")
    summary = []
    for path in paths:
        data = np.fromfile(str(path), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"无法解码：{path.name}")
        height, width = image.shape[:2]
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        mask, patches = heuristic_reflection_mask(rgb, args.threshold, args.min_pixels)
        polygons = mask_to_polygons(mask, min_area=args.min_area)
        document = labelme_document(path.name, width, height, polygons)
        (out / (path.stem + ".json")).write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        preview = image.copy()
        cv2.drawContours(preview, [np.array(polygon, dtype=np.int32) for polygon in polygons], -1, (0, 140, 255), 3)
        imwrite_unicode(out / "previews" / f"{path.stem}__preview.png", preview)
        summary.append({"name": path.name, "polygons": len(polygons), "cue_pixels": float((mask > 0).mean())})
        print(f"{path.name}: {len(polygons)} 个预标注多边形", flush=True)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "photos": len(paths),
              "note": "预标注来自启发式反光线索，只是起点；请在 labelme 中修正、补充、删除，保留 label=water。",
              "files": summary}
    (out / "prelabel_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"完成：{len(paths)} 张 -> {out}（预览在 {out / 'previews'}）")


if __name__ == "__main__":
    main()
