"""Folder-watching demo pipeline: photos in -> ONNX inference -> marked regions -> judgment.

Scans an inbox folder (one-shot or watch mode), runs the ponding ONNX model through
the same runner the web backend uses, draws numbered boxes around each suspected
water patch (the "planned regions"), applies transparent three-level judgment
rules, and writes a standalone HTML gallery plus JSON report into a timestamped
outbox folder. Judgments are leads for manual review, never water-depth or
safety conclusions.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

DEFAULT_MODEL_DIR = Path("artifacts/onnx")
DEFAULT_PYTHON = Path("D:/anaconda/envs/waterseg/python.exe")
DEFAULT_RUNNER = Path(__file__).resolve().parents[2] / "Web/backend/scripts/run_water_onnx.py"
SUPPORTED = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
INTERPRETATION = "判断依据为像素占比与连通斑块数的阈值规则；疑似区域是待人工核查线索，占比不是面积，也不是水深、水位或堤防安全结论。"


def imread_unicode(path: Path) -> np.ndarray | None:
    """cv2.imread cannot handle non-ASCII Windows paths."""
    if not path.is_file():
        return None
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_UNCHANGED)


def imwrite_unicode(path: Path, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError(f"无法编码 PNG：{path}")
    path.write_bytes(encoded.tobytes())

LEVELS = {
    "high": ("疑似积水较多 · 建议现场核查", "#c0290f"),
    "mid": ("少量疑似 · 建议复核照片", "#c98514"),
    "low": ("未见明显疑似", "#2c7a3d"),
}


def judge(fraction: float, patches: int, confidence: float, *, high_fraction: float, high_patches: int, mid_fraction: float) -> tuple[str, str]:
    """Return (level_key, human_text) from transparent thresholds."""
    if fraction >= high_fraction or (patches >= high_patches and fraction >= mid_fraction):
        level = "high"
    elif fraction >= mid_fraction or patches >= 1:
        level = "mid"
    else:
        level = "low"
    text = {
        "high": f"疑似占比 {fraction*100:.1f}%、{patches} 处斑块、平均置信度 {confidence*100:.0f}% → 疑似积水较多，建议现场核查",
        "mid": f"疑似占比 {fraction*100:.1f}%、{patches} 处斑块、平均置信度 {confidence*100:.0f}% → 少量疑似，建议复核照片",
        "low": f"疑似占比 {fraction*100:.1f}%、{patches} 处斑块 → 未见明显疑似",
    }[level]
    return level, text


def draw_boxes(overlay_path: Path, suspected_path: Path, target: Path, min_pixels: int = 40) -> int:
    """Draw numbered rectangles around connected suspected patches; return count kept."""
    overlay = imread_unicode(overlay_path)
    mask = imread_unicode(suspected_path)
    if overlay is not None and overlay.ndim == 3:
        pass
    if mask is not None and mask.ndim == 3:
        mask = mask[:, :, 0]
    if overlay is None or mask is None or overlay.shape[:2] != mask.shape[:2]:
        raise ValueError(f"cannot align overlay/mask for {overlay_path.name}")
    count, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    kept = 0
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if area < min_pixels:
            continue
        kept += 1
        cv2.rectangle(overlay, (x, y), (x + width, y + height), (0, 0, 255), 2)
        label = f"#{kept} {area}px"
        corner = (max(x, 2), max(y - 6, 12))
        cv2.putText(overlay, label, corner, cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(overlay, label, corner, cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1, cv2.LINE_AA)
    imwrite_unicode(target, overlay)
    return kept


def run_runner(python: Path, runner: Path, model_dir: Path, inbox: Path, batch_dir: Path) -> dict:
    command = [str(python), "-B", str(runner), "--model-dir", str(model_dir), "--input-dir", str(inbox), "--output-dir", str(batch_dir)]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    payload = None
    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if "error" in payload or "images" in payload:
                break
    if completed.returncode != 0 or payload is None or "error" in payload:
        raise RuntimeError((payload or {}).get("error") or completed.stderr.strip()[-400:] or "推理进程失败")
    return payload


def html_report(entries: list[dict], model: dict, started: datetime) -> str:
    order = {"high": 0, "mid": 1, "low": 2}
    entries = sorted(entries, key=lambda item: (order[item["level"]], -item["fraction"]))
    counts = {level: sum(1 for entry in entries if entry["level"] == level) for level in order}
    cards = []
    for entry in entries:
        label, color = LEVELS[entry["level"]]
        cards.append(f"""
        <div class="card">
          <div class="thumb"><a href="{entry['boxes']}" target="_blank"><img src="{entry['boxes']}" alt="{entry['name']}"></a></div>
          <div class="meta">
            <strong title="{entry['name']}">{entry['name']}</strong>
            <span class="badge" style="background:{color}">{label}</span>
            <small>{entry['judgment']}</small>
            <small><a href="{entry['overlay']}" target="_blank">叠图</a> · <a href="{entry['suspected']}" target="_blank">掩膜</a> · <a href="{entry['confidence']}" target="_blank">置信度</a> · <a href="{entry['original']}">原图</a></small>
          </div>
        </div>""")
    summary = " · ".join(
        f'<span style="color:{LEVELS[level][1]}">{LEVELS[level][0].split(" · ")[0]} {counts[level]} 张</span>' for level in order
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>飞行后照片积水分析报告 · {started.strftime('%Y-%m-%d %H:%M:%S')}</title>
<style>
 body {{ font-family: "Microsoft YaHei", sans-serif; margin: 24px; background: #f4f6f9; color: #1f2937; }}
 h1 {{ font-size: 20px; }} .summary {{ margin: 8px 0 16px; font-size: 14px; }}
 .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }}
 .card {{ background: #fff; border: 1px solid #dde3ea; border-radius: 10px; overflow: hidden; }}
 .thumb img {{ width: 100%; display: block; }}
 .meta {{ padding: 8px 10px; display: grid; gap: 4px; font-size: 12px; }}
 .meta strong {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
 .badge {{ justify-self: start; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 12px; }}
 .meta small {{ color: #5b6570; line-height: 1.5; }}
 .note {{ margin-top: 16px; padding: 8px 12px; background: #fff7ed; color: #9a5b13; border-radius: 8px; font-size: 12px; }}
 a {{ color: #1d4ed8; }}
</style></head><body>
<h1>飞行后照片积水分析报告</h1>
<div class="summary">共 {len(entries)} 张 · {summary} · 模型 {model.get('default_model')} · 生成于 {started.strftime('%Y-%m-%d %H:%M:%S')}</div>
<div class="grid">{''.join(cards)}</div>
<div class="note">⚠️ {INTERPRETATION}</div>
</body></html>"""


def process_batch(args: argparse.Namespace, inbox: Path, names: list[str]) -> Path | None:
    started = datetime.now()
    batch_dir = args.outbox.resolve() / started.strftime("%Y%m%d_%H%M%S")
    staging = batch_dir / "_in"
    staging.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy2(inbox / name, staging / name)
    payload = run_runner(args.python, args.runner, args.model_dir.resolve(), staging, batch_dir)
    shutil.rmtree(staging)
    (batch_dir / "inputs").mkdir(exist_ok=True)
    for name in names:
        shutil.move(str(inbox / name), str(batch_dir / "inputs" / name))
    entries = []
    for image in payload["images"]:
        boxes_name = Path(image["name"]).stem + "__boxes.png"
        kept = draw_boxes(batch_dir / image["overlay"], batch_dir / image["suspected_mask"], batch_dir / boxes_name)
        level, judgment = judge(image["suspected_fraction"], kept, image["mean_confidence_on_suspected"],
                                 high_fraction=args.high_fraction, high_patches=args.high_patches, mid_fraction=args.mid_fraction)
        entries.append({
            "name": image["name"], "width": image["width"], "height": image["height"],
            "fraction": image["suspected_fraction"], "patches": kept,
            "confidence": image["mean_confidence_on_suspected"],
            "level": level, "judgment": judgment,
            "boxes": boxes_name, "overlay": image["overlay"], "suspected": image["suspected_mask"],
            "confidence_map": image["confidence_map"], "original": "inputs/" + image["name"],
        })
    report = {
        "generated_at": started.isoformat(), "model": payload["model"],
        "thresholds": {"high_fraction": args.high_fraction, "high_patches": args.high_patches, "mid_fraction": args.mid_fraction},
        "images": entries, "interpretation": INTERPRETATION,
    }
    (batch_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (batch_dir / "report.html").write_text(html_report(entries, payload["model"], started), encoding="utf-8")
    counts = {level: sum(1 for entry in entries if entry["level"] == level) for level in ("high", "mid", "low")}
    print(json.dumps({"batch": str(batch_dir), "images": len(entries), "levels": counts,
                      "report": str(batch_dir / "report.html")}, ensure_ascii=False), flush=True)
    return batch_dir


def scan(inbox: Path, seen: set[str]) -> list[str]:
    ready = []
    for path in sorted(inbox.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED or path.name in seen:
            continue
        try:
            stable1, stable2 = path.stat().st_size, path.stat().st_mtime_ns
            time.sleep(0.6)
            if path.stat().st_size == stable1 and path.stat().st_mtime_ns == stable2:
                ready.append(path.name)
        except OSError:
            continue
    return ready


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, default=Path("demo/inbox"))
    parser.add_argument("--outbox", type=Path, default=Path("demo/outbox"))
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--runner", type=Path, default=DEFAULT_RUNNER)
    parser.add_argument("--watch", action="store_true", help="keep scanning every --interval seconds")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--open", action="store_true", help="open each generated report in the browser")
    parser.add_argument("--high-fraction", type=float, default=0.05, help="疑似占比达到该比例判为较多")
    parser.add_argument("--high-patches", type=int, default=3, help="斑块数达到该数量判为较多")
    parser.add_argument("--mid-fraction", type=float, default=0.01, help="疑似占比达到该比例判为少量")
    args = parser.parse_args()
    args.inbox.mkdir(parents=True, exist_ok=True)
    args.outbox.mkdir(parents=True, exist_ok=True)
    if not args.runner.is_file():
        raise SystemExit(f"找不到推理 runner：{args.runner}")
    if not (args.model_dir / "registry.json").is_file():
        raise SystemExit(f"模型目录缺少 registry.json：{args.model_dir}（先运行 waterwatch 导出 ONNX）")
    print(json.dumps({"inbox": str(args.inbox.resolve()), "outbox": str(args.outbox.resolve()),
                      "watch": args.watch, "thresholds": [args.high_fraction, args.high_patches, args.mid_fraction]},
                     ensure_ascii=False), flush=True)
    seen: set[str] = set()
    while True:
        names = scan(args.inbox, seen)
        if names:
            batch = process_batch(args, args.inbox, names)
            seen.update(names)
            if batch and args.open:
                webbrowser.open((batch / "report.html").as_uri())
        if not args.watch:
            if not names:
                print("inbox 中没有可处理的照片。", flush=True)
            break
        time.sleep(max(args.interval, 0.5))


if __name__ == "__main__":
    main()
