"""Post-flight water/ponding segmentation runner executed by the FastAPI backend.

Runs as a standalone process inside the waterseg conda environment (opencv +
numpy + onnxruntime only). Reads every image in --input-dir, runs the ONNX model
described by model_dir/registry.json, and writes per-image artifacts next to a
summary JSON on stdout. This script deliberately has no dependency on the web
server code so it can be replaced or versioned independently.
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
import onnxruntime as ort

SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
INTERPRETATION = "疑似积水像素是待人工核查的线索，不是水深、水位或堤防安全结论；像素占比也不是面积。"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=1, keepdims=True)
    exponent = np.exp(shifted)
    return exponent / exponent.sum(axis=1, keepdims=True)


def load_registry(model_dir: Path) -> dict:
    registry_path = model_dir / "registry.json"
    if not registry_path.is_file():
        raise ValueError(f"模型目录缺少 registry.json：{model_dir}")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for key in ("default_model", "task", "water_class", "input_size", "normalize"):
        if key not in registry:
            raise ValueError(f"registry.json 缺少 {key}")
    model_path = model_dir / registry["default_model"]
    if not model_path.is_file():
        raise ValueError(f"ONNX 模型不存在：{model_path}")
    registry["model_path"] = str(model_path)
    registry["model_sha256"] = sha256_bytes(model_path.read_bytes())
    return registry


def prepare(image: np.ndarray, size: int, normalize: str) -> np.ndarray:
    resized = cv2.resize(image, (size, size), interpolation=cv2.INTER_CUBIC)
    tensor = resized.astype(np.float32) / 255.0
    if normalize == "imagenet":
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        tensor = (tensor - mean) / std
    elif normalize != "div255":
        raise ValueError(f"未知 normalize 策略：{normalize}")
    return tensor.transpose(2, 0, 1)[None, ...]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    try:
        registry = load_registry(args.model_dir)
        session = ort.InferenceSession(registry["model_path"], providers=["CPUExecutionProvider", "CUDAExecutionProvider"])
        input_name = session.get_inputs()[0].name
        water_class = int(registry["water_class"])
        size = int(registry["input_size"])
        args.output_dir.mkdir(parents=True, exist_ok=True)
        images = sorted(path for path in args.input_dir.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES)
        if not images:
            raise ValueError("输入目录没有可识别的图像文件")
        results = []
        for path in images:
            raw = path.read_bytes()
            decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
            if decoded is None:
                raise ValueError(f"无法解码图像：{path.name}")
            rgb = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
            height, width = rgb.shape[:2]
            logits = session.run(["logits"], {input_name: prepare(rgb, size, registry["normalize"])})[0]
            probabilities = softmax(logits[0])
            prediction = np.argmax(logits[0], axis=0).astype(np.uint8)
            confidence = (probabilities[water_class] * 255).round().astype(np.uint8)
            suspected = (prediction == water_class)
            suspected_pixels = int(suspected.sum())
            count, _ = cv2.connectedComponents((suspected * 255).astype(np.uint8), connectivity=8)
            stem = path.stem
            cv2.imwrite(str(args.output_dir / f"{stem}__classes.png"), prediction)
            cv2.imwrite(str(args.output_dir / f"{stem}__suspected.png"), np.where(suspected, 255, 0).astype(np.uint8))
            cv2.imwrite(str(args.output_dir / f"{stem}__confidence.png"), confidence)
            overlay = rgb.copy()
            overlay[suspected] = (30, 110, 255)
            blended = cv2.addWeighted(rgb, 0.6, overlay, 0.4, 0)
            cv2.imwrite(str(args.output_dir / f"{stem}__overlay.png"), cv2.cvtColor(blended, cv2.COLOR_RGB2BGR))
            results.append({
                "name": path.name,
                "sha256": sha256_bytes(raw),
                "width": width,
                "height": height,
                "model_input_size": size,
                "suspected_pixels": suspected_pixels,
                "suspected_fraction": suspected_pixels / float(suspected.size),
                "suspected_patch_count": max(count - 1, 0),
                "mean_confidence_on_suspected": float(confidence[suspected].mean()) if suspected_pixels else 0.0,
                "overlay": f"{stem}__overlay.png",
                "suspected_mask": f"{stem}__suspected.png",
                "confidence_map": f"{stem}__confidence.png",
            })
        payload = {
            "model": {key: registry[key] for key in ("default_model", "task", "water_class", "input_size", "normalize", "model_sha256")},
            "images": results,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "interpretation": INTERPRETATION,
        }
        (args.output_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as error:  # noqa: BLE001 - the caller only parses stdout JSON
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
