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


def softmax(logits: np.ndarray, axis: int = 1) -> np.ndarray:
    shifted = logits - logits.max(axis=axis, keepdims=True)
    exponent = np.exp(shifted)
    return exponent / exponent.sum(axis=axis, keepdims=True)


def class_probabilities(logits: np.ndarray) -> np.ndarray:
    """Return per-class probabilities from ONNX segmentation logits.

    The exported model contract is one batched tensor with shape
    ``[1, classes, height, width]``.  Keeping the batch dimension until after
    softmax makes the class axis explicit; applying softmax to ``logits[0]``
    would normalize each image row instead of the classes.
    """
    logits = np.asarray(logits)
    if logits.ndim != 4 or logits.shape[0] != 1 or any(size < 1 for size in logits.shape[1:]):
        raise ValueError("Expected segmentation logits with shape [1, classes, height, width]")
    return softmax(logits, axis=1)[0]



def imwrite_unicode(path: Path, image: np.ndarray) -> None:
    """cv2.imwrite cannot handle non-ASCII Windows paths; encode then write bytes."""
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError(f"无法编码 PNG：{path}")
    path.write_bytes(encoded.tobytes())

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


def _collect_images(args: argparse.Namespace) -> list[Path]:
    if args.manifest is not None:
        entries = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
            raise ValueError("manifest 必须是图片路径字符串数组。")
        return [Path(entry) for entry in entries]
    return sorted(path for path in args.input_dir.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.manifest is None and args.input_dir is None:
        print(json.dumps({"error": "需要 --manifest 或 --input-dir 之一。"}, ensure_ascii=False))
        return 2
    started = time.perf_counter()
    try:
        registry = load_registry(args.model_dir)
        session = ort.InferenceSession(registry["model_path"], providers=["CPUExecutionProvider", "CUDAExecutionProvider"])
        input_name = session.get_inputs()[0].name
        water_class = int(registry["water_class"])
        size = int(registry["input_size"])
        args.output_dir.mkdir(parents=True, exist_ok=True)
        images = _collect_images(args)
        if not images:
            raise ValueError("输入目录没有可识别的图像文件")
        results = []
        # 逐张追加 results.jsonl：调用方按帧读取进度，单帧失败只写错误行，不中断整批。
        with (args.output_dir / "results.jsonl").open("a", encoding="utf-8") as jsonl:
            for path in images:
                try:
                    record = _predict_one(path, session, input_name, registry, water_class, size, args.output_dir)
                except Exception as error:  # noqa: BLE001 - 单帧失败不拖累队列里其余帧
                    record = {"name": path.name, "error": f"{type(error).__name__}: {error}"[:600]}
                results.append(record)
                jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
                jsonl.flush()
        payload = {
            "model": {key: registry[key] for key in ("default_model", "task", "water_class", "input_size", "normalize", "model_sha256")},
            "images": [item for item in results if "error" not in item],
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "interpretation": INTERPRETATION,
        }
        (args.output_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as error:  # noqa: BLE001 - the caller only parses stdout JSON
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 1


def _predict_one(path: Path, session: "ort.InferenceSession", input_name: str, registry: dict,
                 water_class: int, size: int, output_dir: Path) -> dict:
    raw = path.read_bytes()
    decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValueError(f"无法解码图像：{path.name}")
    rgb = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
    height, width = rgb.shape[:2]
    logits = session.run(["logits"], {input_name: prepare(rgb, size, registry["normalize"])})[0]
    probabilities = class_probabilities(logits)
    prediction = np.argmax(logits[0], axis=0).astype(np.uint8)
    confidence = (probabilities[water_class] * 255).round().astype(np.uint8)
    suspected = (prediction == water_class)
    # Model outputs are model-input sized; scale back to the photo size for display.
    prediction_full = cv2.resize(prediction, (width, height), interpolation=cv2.INTER_NEAREST)
    confidence_full = cv2.resize(confidence, (width, height), interpolation=cv2.INTER_NEAREST)
    suspected_full = prediction_full == water_class
    # Report pixel stats at photo resolution so downstream area math (GSD) is meaningful.
    suspected_pixels = int(suspected_full.sum())
    count, labels = cv2.connectedComponents(suspected_full.astype(np.uint8), connectivity=8)
    patch_area_pixels = [int(area) for area in sorted(np.bincount(labels.ravel())[1:], reverse=True)[:10]]
    stem = path.stem
    imwrite_unicode(output_dir / f"{stem}__classes.png", prediction_full)
    imwrite_unicode(output_dir / f"{stem}__suspected.png", np.where(suspected_full, 255, 0).astype(np.uint8))
    imwrite_unicode(output_dir / f"{stem}__confidence.png", confidence_full)
    overlay = rgb.copy()
    overlay[suspected_full] = (30, 110, 255)
    blended = cv2.addWeighted(rgb, 0.6, overlay, 0.4, 0)
    imwrite_unicode(output_dir / f"{stem}__overlay.png", cv2.cvtColor(blended, cv2.COLOR_RGB2BGR))
    return {
        "name": path.name,
        "sha256": sha256_bytes(raw),
        "width": width,
        "height": height,
        "model_input_size": size,
        "suspected_pixels": suspected_pixels,
        "suspected_fraction": suspected_pixels / float(suspected_full.size),
        "suspected_patch_count": max(count - 1, 0),
        "patch_area_pixels": patch_area_pixels,
        "mean_confidence_on_suspected": float(confidence[suspected].mean()) / 255.0 if suspected_pixels else 0.0,
        "overlay": f"{stem}__overlay.png",
        "suspected_mask": f"{stem}__suspected.png",
        "confidence_map": f"{stem}__confidence.png",
    }


if __name__ == "__main__":
    sys.exit(main())
