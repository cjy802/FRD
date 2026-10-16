"""YOLO-seg / detect comparison baseline over the existing ponding splits.

Builds Ultralytics polygon labels straight from the 0/1 mask folders used by
the ABCDWaveNet experiments (no image copies: splits are referenced via
path-list txt files, and label txts sit in the sibling labels/ directory
required by the ultralytics /images/ -> /labels/ lookup), trains a yolov8-seg
instance segmentation model or a plain yolov8 detector on the same polygon
labels (ultralytics derives boxes from segments for detection), and scores
both with the same water-IoU protocol as evaluate_abcdwavenet: segmentation
predictions merge their instance masks into one binary water mask, while
detection predictions fill their boxes as pseudo masks to quantify how much a
rectangle loses on irregular ponding shapes. ultralytics is imported lazily so
prepare/tests run in the waterseg environment.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

WATER_VALUES = (1, 255)  # same mask convention as evaluate_abcdwavenet

DATASETS = {
    "puddle1000": {
        "train_images": Path("data/abcd/puddle1000/train/images"),
        "train_masks": Path("data/abcd/puddle1000/train/masks"),
        "val_images": Path("data/abcd/puddle1000/val/images"),
        "val_masks": Path("data/abcd/puddle1000/val/masks"),
    },
    "fred": {
        "train_images": Path("data/fred_ft/images"),
        "train_masks": Path("data/fred_ft/masks"),
        "val_images": Path("data/fred_ft/val/images"),
        "val_masks": Path("data/fred_ft/val/masks"),
    },
}


def load_mask(path: Path) -> np.ndarray:
    with Image.open(path) as image_file:
        array = np.asarray(image_file.convert("L"))
    return np.isin(array, WATER_VALUES).astype(np.uint8)


def paired_stems(images_dir: Path, masks_dir: Path) -> list[tuple[str, Path, Path]]:
    pairs = []
    for image_path in sorted(images_dir.iterdir()):
        if not image_path.is_file() or image_path.name.startswith("."):
            continue
        matches = sorted(masks_dir.glob(f"{image_path.stem}.*"))
        if matches:
            pairs.append((image_path.stem, image_path, matches[0]))
    return pairs


def mask_to_polygons(mask: np.ndarray, min_area: int = 30) -> list[list[float]]:
    """Outer-contour polygons normalised to [0, 1]; holes are not represented."""
    height, width = mask.shape[:2]
    contours, _ = cv2.findContours((mask > 0).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    polygons = []
    for contour in contours:
        if cv2.contourArea(contour) < min_area:
            continue
        points = contour.reshape(-1, 2).astype(np.float64)
        points[:, 0] = np.clip(points[:, 0] / width, 0.0, 1.0)
        points[:, 1] = np.clip(points[:, 1] / height, 0.0, 1.0)
        flat = [round(float(value), 6) for value in points.ravel()]
        if len(flat) >= 6:
            polygons.append(flat)
    return polygons


def polygon_to_box(polygon: list[float]) -> list[float]:
    xs = polygon[0::2]
    ys = polygon[1::2]
    return [round((min(xs) + max(xs)) / 2, 6), round((min(ys) + max(ys)) / 2, 6),
            round(max(xs) - min(xs), 6), round(max(ys) - min(ys), 6)]


def polygons_to_mask(polygons: list[list[float]], width: int, height: int) -> np.ndarray:
    canvas = np.zeros((height, width), np.uint8)
    shapes = []
    for polygon in polygons:
        points = np.array([[round(x * width), round(y * height)]
                           for x, y in zip(polygon[0::2], polygon[1::2])], dtype=np.int32)
        if len(points) >= 3:
            shapes.append(points)
    if shapes:
        cv2.fillPoly(canvas, shapes, 1)
    return canvas


def boxes_to_mask(boxes_xyxy: np.ndarray, width: int, height: int) -> np.ndarray:
    """Fill detection boxes as pseudo masks (the rectangle-vs-mask comparison)."""
    canvas = np.zeros((height, width), np.uint8)
    for x1, y1, x2, y2 in np.asarray(boxes_xyxy).reshape(-1, 4):
        left = int(np.clip(round(float(x1)), 0, width - 1))
        top = int(np.clip(round(float(y1)), 0, height - 1))
        right = int(np.clip(round(float(x2)), left + 1, width))
        bottom = int(np.clip(round(float(y2)), top + 1, height))
        canvas[top:bottom, left:right] = 1
    return canvas


def _write_split(task_dir: Path, split: str, labels_dir: Path,
                 pairs: list[tuple[str, Path, Path]], min_area: int) -> dict:
    labels_dir.mkdir(parents=True, exist_ok=True)
    image_lines, polygons_total, background = [], 0, 0
    for stem, image_path, mask_path in pairs:
        polygons = mask_to_polygons(load_mask(mask_path), min_area=min_area)
        lines = [" ".join(["0", *[f"{value:.6f}" for value in polygon]])
                 for polygon in polygons]
        (labels_dir / f"{stem}.txt").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        image_lines.append(str(image_path.resolve()))
        polygons_total += len(polygons)
        background += 0 if lines else 1
    list_path = (task_dir / f"{split}.txt").resolve()
    list_path.write_text("\n".join(image_lines) + "\n", encoding="utf-8")
    return {"images": len(pairs), "polygons": polygons_total, "background_images": background,
            "labels": str(labels_dir), "list": str(list_path)}


def run_prepare(args: argparse.Namespace) -> None:
    layout = DATASETS[args.dataset]
    for key in ("train_images", "train_masks", "val_images", "val_masks"):
        if not layout[key].is_dir():
            raise SystemExit(f"{args.dataset} 数据缺失：{layout[key]}（先完成原有导出/微调数据准备）")
    splits = {
        "train": (paired_stems(layout["train_images"].resolve(), layout["train_masks"].resolve()),
                  layout["train_images"].resolve().parent / "labels"),
        "val": (paired_stems(layout["val_images"].resolve(), layout["val_masks"].resolve()),
                layout["val_images"].resolve().parent / "labels"),
    }
    task_dir = Path("data/yolo") / args.dataset
    task_dir.mkdir(parents=True, exist_ok=True)
    stats = {split: _write_split(task_dir, split, labels_dir, pairs, args.min_area)
             for split, (pairs, labels_dir) in splits.items()}
    yaml_path = task_dir / "data.yaml"
    yaml_path.write_text(
        f"train: {Path(stats['train']['list']).as_posix()}\n"
        f"val: {Path(stats['val']['list']).as_posix()}\n"
        "names:\n  0: water\n", encoding="utf-8")
    print(json.dumps({"data_yaml": str(yaml_path.resolve()),
                      "usable_by": "both seg and detect tasks (boxes derive from segments)",
                      "splits": stats}, ensure_ascii=False, indent=2), flush=True)


def run_train(args: argparse.Namespace) -> None:
    from ultralytics import YOLO

    task = "seg" if "seg" in args.model else "detect"
    data_yaml = (Path("data/yolo") / args.dataset / "data.yaml").resolve()
    if not data_yaml.is_file():
        raise SystemExit(f"{data_yaml} 缺失；先运行 prepare --dataset {args.dataset}")
    weights_dir = (Path("output") / "yolo_seg").resolve()
    weights_dir.mkdir(parents=True, exist_ok=True)
    model = YOLO(str(weights_dir / f"{args.model}.pt"))  # first call downloads to this path
    kwargs = {"data": str(data_yaml), "epochs": args.epochs, "imgsz": args.imgsz, "batch": args.batch,
              "workers": args.workers, "cache": False, "seed": args.seed, "deterministic": True,
              "project": str(weights_dir), "name": args.run_name or f"{args.model}_{args.dataset}",
              "exist_ok": True, "fraction": args.fraction}
    if args.device:
        kwargs["device"] = args.device
    started = time.perf_counter()
    model.train(**kwargs)
    best = Path(model.trainer.best)
    summary = {"task": task, "best_weights": str(best), "elapsed_seconds": round(time.perf_counter() - started, 1),
               "metrics": {key: float(value) for key, value in model.trainer.metrics.items()}}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def _predict_mask(model, image_path: Path, task: str, width: int, height: int,
                  imgsz: int, conf: float) -> np.ndarray:
    result = model.predict(source=str(image_path), imgsz=imgsz, conf=conf,
                           retina_masks=(task == "seg"), verbose=False)[0]
    if task != "seg":
        xyxy = result.boxes.xyxy.cpu().numpy() if result.boxes is not None else np.zeros((0, 4))
        return boxes_to_mask(xyxy, width, height)
    prediction = np.zeros((height, width), np.uint8)
    if result.masks is not None:
        data = result.masks.data
        data = data.cpu().numpy() if hasattr(data, "cpu") else np.asarray(data)
        for instance in data:
            if instance.shape != (height, width):
                instance = cv2.resize(instance.astype(np.uint8), (width, height), interpolation=cv2.INTER_NEAREST)
            prediction |= (instance > 0.5).astype(np.uint8)
    return prediction


def run_evaluate(args: argparse.Namespace) -> None:
    from ultralytics import YOLO

    layout = DATASETS[args.dataset]
    task = args.task or ("seg" if "seg" in Path(args.weights).name else "detect")
    pairs = paired_stems(layout["val_images"].resolve(), layout["val_masks"].resolve())
    if args.limit > 0:
        pairs = pairs[: args.limit]
    if not pairs:
        raise SystemExit(f"{layout['val_images']} 没有成对的图片和掩膜")
    model = YOLO(str(args.weights.resolve()))
    parameters = sum(tensor.numel() for tensor in model.model.parameters())

    confusion = np.zeros((2, 2), dtype=np.int64)
    per_image, overlays = [], []
    started = time.perf_counter()
    for number, (stem, image_path, mask_path) in enumerate(pairs, start=1):
        truth = load_mask(mask_path)
        height, width = truth.shape[:2]
        prediction = _predict_mask(model, image_path, task, width, height, args.imgsz, args.conf)
        confusion += np.bincount(truth.ravel() * 2 + prediction.ravel(), minlength=4).reshape(2, 2)
        tp = int(((truth == 1) & (prediction == 1)).sum())
        union = int(((truth == 1) | (prediction == 1)).sum())
        per_image.append({"stem": stem, "water_iou": tp / union if union else None,
                          "water_truth_fraction": float((truth == 1).mean())})
        if len(overlays) < 5 and (truth == 1).any():
            with Image.open(image_path) as image_file:
                rgb = np.asarray(image_file.convert("RGB")).copy()
            tinted = rgb.copy()
            tinted[prediction == 1] = (30, 110, 255)
            overlays.append((stem, cv2.addWeighted(rgb, 0.6, tinted, 0.4, 0)))
        if number % 50 == 0:
            print(f"scored {number}/{len(pairs)}", flush=True)
    elapsed = time.perf_counter() - started

    tp, fp, fn = float(confusion[1, 1]), float(confusion[0, 1]), float(confusion[1, 0])
    water = {"iou": tp / (tp + fp + fn) if tp + fp + fn else None,
             "precision": tp / (tp + fp) if tp + fp else None,
             "recall": tp / (tp + fn) if tp + fn else None,
             "dice": 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None}
    ious = [entry["water_iou"] for entry in per_image if entry["water_iou"] is not None]

    native: dict = {}
    if not args.skip_native_val:
        data_yaml = (Path("data/yolo") / args.dataset / "data.yaml").resolve()
        try:
            metrics = model.val(data=str(data_yaml), imgsz=args.imgsz, workers=2, verbose=False)
            for name, group in (("box", getattr(metrics, "box", None)), ("mask", getattr(metrics, "seg", None))):
                if group is not None:
                    native[f"{name}_mAP50"] = float(group.map50)
        except Exception as error:  # native numbers are reference-only
            native["error"] = repr(error)

    output = args.output or Path(f"output/reproduction_yolo{task}_{args.dataset}")
    output.mkdir(parents=True, exist_ok=True)
    for stem, image in overlays:
        cv2.imwrite(str(output / f"overlay_{stem}.png"), cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    report = {
        "task": f"road ponding YOLO {task} baseline (ultralytics {Path(args.weights).name})",
        "weights": str(Path(args.weights).resolve()),
        "task_type": task,
        "dataset": args.dataset,
        "mask_merge": "union of instance masks" if task == "seg" else "boxes filled as pseudo masks",
        "predict_conf": args.conf,
        "predict_imgsz": args.imgsz,
        "parameters": parameters,
        "images": len(pairs),
        "evaluated_pixels": int(confusion.sum()),
        "water_metrics": water,
        "confusion_rows_true_columns_predicted_background_water": confusion.tolist(),
        "per_image_water_iou": {"median": float(np.median(ious)) if ious else None,
                                "min": float(min(ious)) if ious else None,
                                "max": float(max(ious)) if ious else None},
        "ultralytics_native_val_mAP50": native or None,
        "inference_seconds_per_image": round(elapsed / max(len(pairs), 1), 3),
        "elapsed_seconds": round(elapsed, 1),
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"task_type": task, "water_iou": water["iou"], "water_dice": water["dice"],
                      "images": len(pairs), "native_mAP50": native or None,
                      "report": str(output / "report.json")}, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="build YOLO labels/data.yaml from the 0/1 mask folders")
    prepare.add_argument("--dataset", choices=sorted(DATASETS), required=True)
    prepare.add_argument("--min-area", type=int, default=30, help="drop polygon speckles below this pixel area")
    prepare.set_defaults(func=run_prepare)

    train = subparsers.add_parser("train", help="train a yolov8(-seg) model on the prepared split")
    train.add_argument("--dataset", choices=sorted(DATASETS), required=True)
    train.add_argument("--model", default="yolov8n-seg",
                       help="ultralytics model name; containing 'seg' selects segmentation, else detection")
    train.add_argument("--epochs", type=int, default=100)
    train.add_argument("--batch", type=int, default=8)
    train.add_argument("--imgsz", type=int, default=640)
    train.add_argument("--workers", type=int, default=2)
    train.add_argument("--seed", type=int, default=42)
    train.add_argument("--fraction", type=float, default=1.0, help="train on this fraction of the split (smoke tests)")
    train.add_argument("--device", default="", help="e.g. 0 or cpu; empty lets ultralytics pick")
    train.add_argument("--run-name", default=None)
    train.set_defaults(func=run_train)

    evaluate = subparsers.add_parser("evaluate", help="score weights with the shared water-IoU protocol")
    evaluate.add_argument("--dataset", choices=sorted(DATASETS), required=True)
    evaluate.add_argument("--weights", type=Path, required=True)
    evaluate.add_argument("--task", choices=("seg", "detect"), default=None,
                          help="default: inferred from the weights file name")
    evaluate.add_argument("--conf", type=float, default=0.25)
    evaluate.add_argument("--imgsz", type=int, default=640)
    evaluate.add_argument("--limit", type=int, default=0)
    evaluate.add_argument("--output", type=Path, default=None)
    evaluate.add_argument("--skip-native-val", action="store_true", help="skip the ultralytics mAP reference run")
    evaluate.set_defaults(func=run_evaluate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
