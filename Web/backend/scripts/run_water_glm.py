"""Ask a vision model for normalized puddle polygons and rasterize them locally.

复检（manifest 条目带 ``mask``）时，按第一遍 YOLO 的疑似掩膜取连通域 bbox、
外扩裁块后送检，多边形坐标映射回全帧再栅格化；无掩膜的条目整帧送检。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np
import requests


def _endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("远程模型 URL 必须是有效的 HTTPS 地址。")
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _json_content(content: object) -> dict:
    if isinstance(content, list):
        content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    if not isinstance(content, str):
        raise ValueError("模型没有返回可读取的分割结果文本。")
    cleaned = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("模型没有返回 JSON 多边形；请确认该模型端点支持图像输入和结构化输出。")
    payload = json.loads(cleaned[start:end + 1])
    if not isinstance(payload, dict) or not isinstance(payload.get("detections"), list):
        raise ValueError("模型结果缺少 detections 多边形数组。")
    return payload


def _polygon_points(raw: object, width: int, height: int) -> np.ndarray | None:
    if not isinstance(raw, list) or len(raw) < 3:
        return None
    points = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            return None
        try:
            x, y = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            return None
        if not np.isfinite(x) or not np.isfinite(y) or x < -0.02 or x > 1.02 or y < -0.02 or y > 1.02:
            return None
        points.append((round(min(max(x, 0), 1) * (width - 1)), round(min(max(y, 0), 1) * (height - 1))))
    if len(set(points)) < 3:
        return None
    return np.asarray(points, dtype=np.int32)


def _read_image(path: Path) -> np.ndarray:
    """Decode image bytes directly so OpenCV also supports Unicode Windows paths."""
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ValueError(f"无法读取截图：{path.name}") from error
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法读取截图：{path.name}")
    return image


def _write_png(path: Path, image: np.ndarray) -> None:
    """Encode first, then write via pathlib to avoid OpenCV Unicode path limits."""
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError(f"无法写入分析结果：{path.name}")
    path.write_bytes(encoded.tobytes())


def _call_model(image: np.ndarray, url: str, api_key: str, model: str, cropped: bool = False) -> dict:
    height, width = image.shape[:2]
    max_side = 1536
    scale = min(1.0, max_side / max(width, height))
    request_image = image if scale == 1 else cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", request_image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError("无法压缩当前视频截图。")
    image_url = "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii")
    scope = "这是无人机道路照片中一个疑似区域的局部裁剪。" if cropped else "这是一张无人机道路照片。"
    prompt = (
        f"{scope}只识别画面里可见的积水/水坑区域。不要把阴影、车身、反光、"
        "湿润但没有积水的路面或天空识别成水坑。请只返回 JSON，不要 Markdown 或解释，格式为："
        '{"detections":[{"label":"puddle","confidence":0.0,"polygon":[[0.1,0.2],[0.3,0.2],[0.3,0.4]]}]}。'
        "polygon 使用相对原图宽高归一化到 0–1 的边界点，原点在左上角；只在能确定水坑边界时输出，"
        "没有水坑时返回空 detections。建议每个明显独立水坑一个多边形，最多 30 个。"
    )
    try:
        # 本接口是国内直连端点；系统代理/VPN 常导致 ProxyError，这里显式绕开。
        session = requests.Session()
        session.trust_env = False
        response = session.post(
            _endpoint(url),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "stream": False,
                "temperature": 0.1,
                "max_tokens": 3500,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]}],
            },
            timeout=(15, 180),
        )
    except requests.RequestException as error:
        raise RuntimeError(f"远程图像请求失败：{type(error).__name__}。") from error
    if not response.ok:
        try:
            detail = response.json().get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            detail = ""
        detail = str(detail)[:240]
        if api_key:
            detail = detail.replace(api_key, "[已隐藏 API Key]")
        raise RuntimeError(f"模型接口返回 HTTP {response.status_code}：{detail or '请检查 URL、Key、模型权限和图像输入能力。'}")
    try:
        answer = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise RuntimeError("模型接口响应格式不包含 choices.message.content。") from error
    return _json_content(answer)


# --- 裁块送检策略 -----------------------------------------------------------
REGION_EXPAND = 1.5        # 疑似区域外扩倍数：保留周边路面给模型做上下文
REGION_MIN_SIDE = 256      # 裁块最短边下限：太小补齐，避免模型失去参照物
REGION_MIN_AREA = 64       # 小于该像素面积的连通域按噪声忽略
REGION_MAX_COUNT = 4       # 每帧最多送检裁块数，多余区域就近合并，控住调用成本


def _expand_region(x: int, y: int, w: int, h: int, width: int, height: int) -> list[int]:
    center_x, center_y = x + w / 2, y + h / 2
    half_w = max(w * REGION_EXPAND / 2, REGION_MIN_SIDE / 2)
    half_h = max(h * REGION_EXPAND / 2, REGION_MIN_SIDE / 2)
    x0 = max(int(center_x - half_w), 0)
    y0 = max(int(center_y - half_h), 0)
    x1 = min(int(center_x + half_w) + 1, width)
    y1 = min(int(center_y + half_h) + 1, height)
    return [x0, y0, x1, y1]


def _region_gap(a: list[int], b: list[int]) -> int:
    gap_x = max(b[0] - a[2], a[0] - b[2], 0)
    gap_y = max(b[1] - a[3], a[1] - b[3], 0)
    return gap_x + gap_y


def _merge_regions(boxes: list[list[int]], max_count: int) -> list[list[int]]:
    """重叠区域先合并；数量仍超限时不断合并相距最近的两块，保证不丢候选。"""
    boxes = [list(box) for box in boxes]
    while len(boxes) > 1 and (len(boxes) > max_count
                              or any(_region_gap(boxes[i], boxes[j]) == 0
                                     for i in range(len(boxes))
                                     for j in range(i + 1, len(boxes)))):
        best_pair, best_gap = None, None
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                gap = _region_gap(boxes[i], boxes[j])
                if best_gap is None or gap < best_gap:
                    best_pair, best_gap = (i, j), gap
        i, j = best_pair
        boxes[i] = [min(boxes[i][0], boxes[j][0]), min(boxes[i][1], boxes[j][1]),
                    max(boxes[i][2], boxes[j][2]), max(boxes[i][3], boxes[j][3])]
        boxes.pop(j)
    return boxes


def _suspected_regions(mask: np.ndarray, width: int, height: int) -> list[list[int]]:
    """从第一遍 YOLO 的疑似掩膜取连通域 bbox，外扩成送检裁块。"""
    if mask.shape[:2] != (height, width):
        mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
    count, _, stats, _ = cv2.connectedComponentsWithStats((mask > 127).astype(np.uint8), connectivity=8)
    boxes = []
    for index in range(1, count):
        x, y, w, h, area = (int(value) for value in stats[index])
        if area < REGION_MIN_AREA:
            continue
        boxes.append(_expand_region(x, y, w, h, width, height))
    if not boxes:
        return []
    return _merge_regions(boxes, REGION_MAX_COUNT)


def _read_mask(path: Path | None) -> np.ndarray | None:
    if path is None:
        return None
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    return cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)


def _process(entry: dict, output_dir: Path, url: str, api_key: str, model: str) -> dict:
    path = entry["path"]
    image = _read_image(path)
    height, width = image.shape[:2]
    mask_source = _read_mask(entry.get("mask"))
    regions = _suspected_regions(mask_source, width, height) if mask_source is not None else []
    cropped = bool(regions)
    if not regions:
        regions = [[0, 0, width, height]]
    mask = np.zeros((height, width), dtype=np.uint8)
    confidence = np.zeros((height, width), dtype=np.uint8)
    valid = []
    region_errors: list[str] = []
    for x0, y0, x1, y1 in regions:
        crop = image[y0:y1, x0:x1]
        try:
            result = _call_model(crop, url, api_key, model, cropped)
        except ValueError as first_error:
            # Vision chat models can occasionally ignore the JSON-only instruction.
            # Retry once so one malformed response does not discard an otherwise valid batch.
            try:
                result = _call_model(crop, url, api_key, model, cropped)
            except ValueError as retry_error:
                region_errors.append(f"裁块({x0},{y0},{x1},{y1})两次均未返回有效 JSON：{retry_error}")
                continue
        for item in result["detections"][:30]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "puddle")).strip().lower()
            if label not in {"puddle", "water", "flood", "积水", "水坑"}:
                continue
            try:
                score = min(max(float(item.get("confidence", 0.5)), 0), 1)
            except (TypeError, ValueError):
                score = 0.5
            polygon_local = _polygon_points(item.get("polygon"), x1 - x0, y1 - y0)
            if polygon_local is None:
                continue
            polygon = polygon_local + np.asarray([x0, y0], dtype=np.int32)
            cv2.fillPoly(mask, [polygon], 255)
            instance_mask = np.zeros_like(mask)
            cv2.fillPoly(instance_mask, [polygon], 255)
            confidence[instance_mask > 0] = np.maximum(confidence[instance_mask > 0], round(score * 255))
            valid.append({"label": "puddle", "confidence": round(score, 4), "region": [x0, y0, x1, y1]})
    if len(region_errors) == len(regions):
        # 所有裁块都失败才按失败帧处理，交给队列重试；部分失败仍产出结果。
        raise ValueError("；".join(region_errors))
    overlay = image.copy()
    tint = np.zeros_like(image)
    tint[:, :] = (255, 120, 40)
    selected = mask > 0
    blended = cv2.addWeighted(image, 0.45, tint, 0.55, 0)
    overlay[selected] = blended[selected]
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    overlay_name = f"{stem}-overlay.png"
    mask_name = f"{stem}-mask.png"
    confidence_name = f"{stem}-confidence.png"
    _write_png(output_dir / overlay_name, overlay)
    _write_png(output_dir / mask_name, mask)
    _write_png(output_dir / confidence_name, confidence)
    components, labels = cv2.connectedComponents((mask > 0).astype(np.uint8))
    patch_area_pixels = [int(area) for area in sorted(np.bincount(labels.ravel())[1:], reverse=True)[:10]]
    pixels = int(np.count_nonzero(mask))
    return {
        "name": path.name,
        "original_name": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "width": width,
        "height": height,
        "model_input_size": max(width, height),
        "recheck_mode": "cropped" if cropped else "full",
        "regions": regions,
        "suspected_pixels": pixels,
        "suspected_fraction": pixels / (width * height),
        "suspected_patch_count": max(components - 1, 0),
        "patch_area_pixels": patch_area_pixels,
        "mean_confidence_on_suspected": round(float(confidence[selected].mean()) / 255, 4) if pixels else 0.0,
        "instance_count": len(valid),
        "overlay": overlay_name,
        "suspected_mask": mask_name,
        "confidence_map": confidence_name,
        **({"region_errors": region_errors} if region_errors else {}),
    }


def _collect_images(args: argparse.Namespace) -> list[dict]:
    """manifest 条目兼容两种形态：路径字符串，或 {path, mask} 对象（复检裁块）。"""

    def _entry(raw: object) -> dict:
        if isinstance(raw, str):
            return {"path": Path(raw), "mask": None}
        if isinstance(raw, dict) and isinstance(raw.get("path"), str):
            mask = raw.get("mask")
            return {"path": Path(raw["path"]),
                    "mask": Path(mask) if isinstance(mask, str) and mask else None}
        raise ValueError("manifest 条目必须是路径字符串或 {path, mask} 对象。")

    suffixes = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    if args.manifest is not None:
        entries = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("manifest 必须是 JSON 数组。")
        return [_entry(entry) for entry in entries]
    return [{"path": path, "mask": None} for path in sorted(
        path for path in args.input_dir.iterdir() if path.is_file() and path.suffix.lower() in suffixes)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.manifest is None and args.input_dir is None:
        print(json.dumps({"error": "需要 --manifest 或 --input-dir 之一。"}, ensure_ascii=False))
        return 2
    url = os.environ.get("WATER_ANALYSIS_REMOTE_URL", "").strip()
    api_key = os.environ.get("WATER_ANALYSIS_REMOTE_API_KEY", "").strip()
    model = os.environ.get("WATER_ANALYSIS_REMOTE_MODEL", "glm-5.3-flash").strip()
    if not url or not api_key or not model:
        print(json.dumps({"error": "远程模型配置不完整，请检查后端 .env。"}, ensure_ascii=False))
        return 2
    try:
        image_paths = _collect_images(args)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": f"无法读取图片清单：{error}"}, ensure_ascii=False))
        return 2
    if not image_paths:
        print(json.dumps({"error": "没有可分析的视频截图。"}, ensure_ascii=False))
        return 2
    started = time.monotonic()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    # 逐张追加 results.jsonl：调用方按帧读取进度，单帧失败只写错误行，不中断整批。
    with (args.output_dir / "results.jsonl").open("a", encoding="utf-8") as jsonl:
        for entry in image_paths:
            try:
                record = _process(entry, args.output_dir, url, api_key, model)
            except Exception as error:  # noqa: BLE001 - 单帧失败不拖累队列里其余帧
                detail = str(error).replace(api_key, "[已隐藏 API Key]") if api_key else str(error)
                record = {"name": entry["path"].name, "error": detail[:600]}
            jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
            jsonl.flush()
            if "error" not in record:
                entries.append(record)
    model_digest = hashlib.sha256(model.encode("utf-8")).hexdigest()
    payload = {
        "model": {"default_model": model, "display_name": model, "engine": "remote-vision-polygon-mask",
                  "task": "puddle candidate segmentation", "water_class": 1,
                  "input_size": 1536, "normalize": "remote vision request; normalized polygons rasterized locally",
                  "region_policy": {"expand": REGION_EXPAND, "min_side": REGION_MIN_SIDE,
                                    "max_regions_per_frame": REGION_MAX_COUNT,
                                    "min_component_area": REGION_MIN_AREA},
                  "confidence_threshold": None, "model_sha256": model_digest},
        "images": entries,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "interpretation": "GLM 返回的多边形已在本机栅格化为候选掩膜；带疑似掩膜的帧按外扩裁块送检，其余整帧送检。"
                          "多边形精度受语言视觉模型限制，所有区域都需要人工核对，不代表积水深度或面积。",
    }
    (args.output_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
