"""Command-line entry points; all real data is supplied by the user."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import CLASSES, MODES
from .annotations import labelme_to_mask
from .flood import shoreline_change
from .images import load_scene, read_event, read_mask, read_metadata, save_json, source_record
from .learning import evaluate_model, load_model, save_prediction, train_model
from .manifest import read_manifest
from .ponding import summarize_ponding
from .plan import read_sites, regional_plan


def _path(value: str) -> Path:
    return Path(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="低空水患巡查 RGB／二维热图试验原型")
    commands = parser.add_subparsers(dest="command", required=True)

    train = commands.add_parser("train", help="按任务和输入模式训练一个独立的实验基线")
    train.add_argument("--manifest", type=_path, required=True)
    train.add_argument("--task", choices=tuple(CLASSES), required=True)
    train.add_argument("--mode", choices=MODES, required=True)
    train.add_argument("--output", type=_path, required=True)
    train.add_argument("--max-edge", type=int, default=1280, help="训练前最长边缩放；默认 1280 像素")
    train.add_argument("--max-per-class", type=int, default=3000, help="每场景每类最多抽样像素")
    train.add_argument("--paired-support", action="store_true", help="RGB 对照模型仅用热图重叠区训练；用于公平对比三模式")

    evaluate = commands.add_parser("evaluate", help="仅在留出场景 val/test 上评估")
    evaluate.add_argument("--manifest", type=_path, required=True)
    evaluate.add_argument("--model", type=_path, required=True)
    evaluate.add_argument("--split", choices=("val", "test"), default="test")
    evaluate.add_argument("--output", type=_path, required=True)

    predict = commands.add_parser("predict", help="预测一张新图，保存类别、疑点和证据报告")
    predict.add_argument("--model", type=_path, required=True)
    predict.add_argument("--rgb", type=_path, required=True)
    predict.add_argument("--thermal", type=_path)
    predict.add_argument("--registration", help="热图到 RGB 的 JSON 配准文件，或 aligned")
    predict.add_argument("--metadata", type=_path, required=True)
    predict.add_argument("--roi", type=_path, required=True, help="同尺寸单通道 ROI；非零=可检查区域")
    predict.add_argument("--event", type=_path)
    predict.add_argument("--output", type=_path, required=True)

    change = commands.add_parser("shoreline-change", help="同岸段两次水陆掩膜变化；不推算绝对水位")
    change.add_argument("--before-rgb", type=_path, required=True)
    change.add_argument("--before-mask", type=_path, required=True)
    change.add_argument("--before-metadata", type=_path, required=True)
    change.add_argument("--after-rgb", type=_path, required=True)
    change.add_argument("--after-mask", type=_path, required=True)
    change.add_argument("--after-metadata", type=_path, required=True)
    alignment = change.add_mutually_exclusive_group(required=True)
    alignment.add_argument("--registration", type=_path, help="after 到 before 的控制点 JSON")
    alignment.add_argument("--same-view", action="store_true", help="明确确认两次图片像素坐标一致")
    change.add_argument("--event", type=_path)
    change.add_argument("--min-component-pixels", type=int, default=1)
    change.add_argument("--before-mask-origin", choices=("manual", "model", "unverified"), default="unverified")
    change.add_argument("--after-mask-origin", choices=("manual", "model", "unverified"), default="unverified")
    change.add_argument("--output", type=_path, required=True)

    plan = commands.add_parser("plan", help="按人工录入的官方事件适用区域生成待巡查点清单")
    plan.add_argument("--event", type=_path, required=True)
    plan.add_argument("--sites", type=_path, required=True)
    plan.add_argument("--task", choices=tuple(CLASSES))
    plan.add_argument("--output", type=_path, required=True)

    labels = commands.add_parser("mask-from-labelme", help="把 Labelme 多边形标注转换成索引掩膜或 ROI")
    labels.add_argument("--json", type=_path, required=True)
    labels.add_argument("--rgb", type=_path, required=True)
    labels.add_argument("--task", choices=("ponding", "shoreline", "roi"), required=True)
    labels.add_argument("--output", type=_path, required=True)
    return parser


def run(arguments: argparse.Namespace) -> dict:
    if arguments.command == "train":
        return train_model(
            read_manifest(arguments.manifest), arguments.task, arguments.mode, arguments.output,
            max_edge=arguments.max_edge, max_per_class=arguments.max_per_class, paired_support=arguments.paired_support,
        )
    if arguments.command == "evaluate":
        result = evaluate_model(read_manifest(arguments.manifest), load_model(arguments.model), split=arguments.split)
        save_json(arguments.output, result)
        return result
    if arguments.command == "predict":
        bundle = load_model(arguments.model)
        if bundle["mode"] == "rgb" and (arguments.thermal or arguments.registration):
            raise ValueError("RGB 模型不会使用热图；请移除热图参数或加载 fusion/thermal 模型")
        if bundle["mode"] != "rgb" and arguments.thermal is None:
            raise ValueError(f"{bundle['mode']} 模型需要 --thermal 和 --registration")
        registration = arguments.registration
        if registration and registration != "aligned":
            registration = Path(registration)
        scene = load_scene(arguments.rgb, thermal_path=arguments.thermal, thermal_registration=registration)
        metadata = read_metadata(arguments.metadata)
        event = read_event(arguments.event) if arguments.event else None
        roi = read_mask(arguments.roi, scene.rgb.shape[:2])
        sources = {
            "rgb": source_record(arguments.rgb),
            "thermal": source_record(arguments.thermal),
            "thermal_registration": source_record(registration) if isinstance(registration, Path) else None,
            "roi": source_record(arguments.roi),
            "metadata": source_record(arguments.metadata),
            "event": source_record(arguments.event),
            "model": source_record(arguments.model),
        }
        result = save_prediction(arguments.output, bundle, scene, metadata, roi, event, sources)
        if bundle["task"] == "ponding":
            result["ponding_summary"] = summarize_ponding(read_mask(arguments.output / "classes.png"))
            save_json(arguments.output / "report.json", result)
        return result
    if arguments.command == "shoreline-change":
        return shoreline_change(
            arguments.before_rgb, arguments.before_mask, arguments.after_rgb, arguments.after_mask,
            read_metadata(arguments.before_metadata), read_metadata(arguments.after_metadata), arguments.output,
            registration=arguments.registration, same_view=arguments.same_view,
            event=read_event(arguments.event) if arguments.event else None,
            min_component_pixels=arguments.min_component_pixels,
            before_mask_origin=arguments.before_mask_origin,
            after_mask_origin=arguments.after_mask_origin,
        )
    if arguments.command == "plan":
        result = regional_plan(read_event(arguments.event), read_sites(arguments.sites), arguments.task)
        save_json(arguments.output, result)
        return result
    if arguments.command == "mask-from-labelme":
        return labelme_to_mask(arguments.json, arguments.rgb, arguments.task, arguments.output)
    raise AssertionError(f"未知命令：{arguments.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        result = run(parser.parse_args(argv))
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"错误：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
