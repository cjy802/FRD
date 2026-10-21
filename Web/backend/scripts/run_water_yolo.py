"""Run the archived YOLOv8n-seg water checkpoint over uploaded photos.

This subprocess runs in the Ultralytics environment so the FastAPI process does
not import PyTorch. It writes the same artifact contract as run_water_onnx.py:
an overlay, binary water mask, confidence map, and a JSON summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
IMAGE_SIZE = 640
CONFIDENCE_THRESHOLD = 0.25
INTERPRETATION = (
    "该模型输出可见水体候选掩膜，适合辅助查看路面积水和洪水场景。"
    "本地种子照片参与过训练，结果只作待人工核查线索；像素占比不是面积，也不是水深、水位或堤防安全结论。"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def imwrite_unicode(path: Path, image: np.ndarray) -> None:
    """Write a PNG without relying on OpenCV's non-ASCII path handling."""
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError(f"无法编码 PNG：{path}")
    path.write_bytes(encoded.tobytes())


def load_bgr(path: Path) -> np.ndarray:
    decoded = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValueError(f"无法解码图像：{path.name}")
    return decoded


def masks_and_confidence(result, width: int, height: int) -> tuple[np.ndarray, np.ndarray, int]:
    water = np.zeros((height, width), dtype=bool)
    confidence = np.zeros((height, width), dtype=np.float32)
    if result.masks is None:
        return water, confidence, 0

    masks = result.masks.data.detach().cpu().numpy()
    scores = result.boxes.conf.detach().cpu().numpy() if result.boxes is not None else np.zeros((0,))
    instance_count = 0
    for index, mask in enumerate(masks):
        if mask.shape != (height, width):
            mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
        active = mask > 0.5
        if not active.any():
            continue
        water |= active
        score = float(scores[index]) if index < len(scores) else 0.0
        np.maximum(confidence, score, out=confidence, where=active)
        instance_count += 1
    return water, confidence, instance_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    try:
        if not args.weights.is_file():
            raise FileNotFoundError(f"YOLO 权重不存在：{args.weights}")
        model = YOLO(str(args.weights))
        model_sha256 = sha256_file(args.weights)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        images = sorted(path for path in args.input_dir.iterdir()
                        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES)
        if not images:
            raise ValueError("输入目录没有可识别的图像文件")

        results = []
        for path in images:
            bgr = load_bgr(path)
            height, width = bgr.shape[:2]
            prediction = model.predict(
                source=bgr,
                imgsz=IMAGE_SIZE,
                conf=CONFIDENCE_THRESHOLD,
                retina_masks=True,
                verbose=False,
            )[0]
            water, confidence, instance_count = masks_and_confidence(prediction, width, height)
            suspected_pixels = int(water.sum())
            count, labels = cv2.connectedComponents(water.astype(np.uint8), connectivity=8)
            patch_area_pixels = [int(area) for area in sorted(np.bincount(labels.ravel())[1:], reverse=True)[:10]]

            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            overlay = rgb.copy()
            overlay[water] = (30, 110, 255)
            blended = cv2.addWeighted(rgb, 0.6, overlay, 0.4, 0)
            stem = path.stem
            imwrite_unicode(args.output_dir / f"{stem}__suspected.png", np.where(water, 255, 0).astype(np.uint8))
            imwrite_unicode(args.output_dir / f"{stem}__confidence.png", np.round(confidence * 255).astype(np.uint8))
            imwrite_unicode(args.output_dir / f"{stem}__overlay.png", cv2.cvtColor(blended, cv2.COLOR_RGB2BGR))
            results.append({
                "name": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "width": width,
                "height": height,
                "model_input_size": IMAGE_SIZE,
                "suspected_pixels": suspected_pixels,
                "suspected_fraction": suspected_pixels / float(water.size),
                "suspected_patch_count": max(count - 1, 0),
                "patch_area_pixels": patch_area_pixels,
                "mean_confidence_on_suspected": float(confidence[water].mean()) if suspected_pixels else 0.0,
                "instance_count": instance_count,
                "overlay": f"{stem}__overlay.png",
                "suspected_mask": f"{stem}__suspected.png",
                "confidence_map": f"{stem}__confidence.png",
            })

        payload = {
            "model": {
                "default_model": args.weights.name,
                "display_name": "YOLOv8n-seg 4090 混训冠军",
                "engine": "Ultralytics",
                "task": "binary_water_instance_segmentation",
                "water_class": 0,
                "input_size": IMAGE_SIZE,
                "normalize": "ultralytics",
                "confidence_threshold": CONFIDENCE_THRESHOLD,
                "model_sha256": model_sha256,
            },
            "images": results,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "interpretation": INTERPRETATION,
        }
        (args.output_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                                      encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as error:  # noqa: BLE001 - caller stores this as the job error
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
