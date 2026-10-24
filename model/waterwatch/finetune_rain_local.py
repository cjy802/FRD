"""Fine-tune the ponding champion on the two rain photos locally (RTX 4060).

Warm-starts from output/4090_champions/yoloj_mix4_best.pt on a small replay
mix: the two rain photos (data/rain_seed rain_photo_campus/rain_photo_avenue)
oversampled x40 with horizontal flips, the original 3-photo local seed, and a
seeded sample of the Puddle/FRED/FloodNet train pools so the replay seesaw
(REPRODUCTION.md 11c) does not wash out the existing domains. Val mixes the
two rain originals with domain samples as a forgetting watchdog.

Outputs to output/rain_ft/ (never touches the champion weights). Preview
overlays for the two rain photos plus two unlabelled rain frames land in
output/rain_ft/preview/ for eyeball acceptance.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import time
from pathlib import Path

import cv2
import numpy as np

from .yolo_seg_baseline import load_mask, mask_to_polygons

ROOT = Path(__file__).resolve().parents[1]
CHAMPION = ROOT / "output" / "4090_champions" / "yoloj_mix4_best.pt"
RAIN_SEED = ROOT / "data" / "rain_seed"
LOCAL_SEED = ROOT / "data" / "local_seed"
REPLAY_POOLS = [
    ("puddle", ROOT / "data" / "abcd" / "puddle1000" / "train", 120),
    ("fred", ROOT / "data" / "fred_ft", 120),
    ("floodnet", ROOT / "data" / "floodnet" / "train", 80),
]
VAL_POOLS = [
    ("puddle", ROOT / "data" / "abcd" / "puddle1000" / "val", 30),
    ("fred", ROOT / "data" / "fred_ft" / "val", 30),
    ("floodnet", ROOT / "data" / "floodnet" / "val", 20),
]
RAIN_PHOTOS = ["rain_photo_campus", "rain_photo_avenue", "rain_photo_street"]
RAIN_FRAMES = ["rain_concat_001", "rain_concat_006", "rain_concat_011", "rain_concat_016",
               "rain_snow_001", "rain_snow_004", "rain_snow_011"]
OVERSAMPLE_PHOTOS = 30  # per rain photo, plus mirrored copies
OVERSAMPLE_FRAMES = 10  # per video frame (frames within one video are near-duplicates)


def _pairs(images_dir: Path, masks_dir: Path, limit: int, rng: random.Random) -> list[Path]:
    stems = sorted({p.stem for p in images_dir.iterdir()
                    if p.suffix.lower() in {".png", ".jpg", ".jpeg"}
                    and (masks_dir / f"{p.stem}.png").is_file()})
    if limit and len(stems) > limit:
        stems = rng.sample(stems, limit)
    return [images_dir / f"{stem}{next(p.suffix for p in images_dir.iterdir() if p.stem == stem)}"
            for stem in stems]


def _stage_entry(dst_images: Path, dst_masks: Path, stem: str, image: np.ndarray, mask: np.ndarray) -> None:
    cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tofile(str(dst_images / f"{stem}.jpg"))
    cv2.imencode(".png", (mask > 0).astype(np.uint8) * 255)[1].tofile(str(dst_masks / f"{stem}.png"))


def build_mix(work: Path) -> dict[str, Path]:
    rng = random.Random(20261024)
    train_dir = work / "train"
    val_dir = work / "val"
    for folder in (train_dir, val_dir):
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "images").mkdir(parents=True)
        (folder / "masks").mkdir(parents=True)

    train_count = 0
    # 照片 ×30 + 水平翻转 ×30：照片是独立场景，新域梯度主力。
    for name in RAIN_PHOTOS:
        image = cv2.imdecode(np.frombuffer((RAIN_SEED / "images" / f"{name}.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        mask = load_mask(RAIN_SEED / "masks" / f"{name}.png")
        flipped = (image[:, ::-1].copy(), mask[:, ::-1].copy())
        for copy_index in range(OVERSAMPLE_PHOTOS):
            _stage_entry(train_dir / "images", train_dir / "masks", f"{name}_r{copy_index:02d}", image, mask)
            _stage_entry(train_dir / "images", train_dir / "masks", f"{name}_f{copy_index:02d}", *flipped)
            train_count += 2
    # 视频帧 ×10 + 翻转 ×10：同视频帧近似重复，系数压低。
    for name in RAIN_FRAMES:
        image = cv2.imdecode(np.frombuffer((RAIN_SEED / "images" / f"{name}.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        mask = load_mask(RAIN_SEED / "masks" / f"{name}.png")
        flipped = (image[:, ::-1].copy(), mask[:, ::-1].copy())
        for copy_index in range(OVERSAMPLE_FRAMES):
            _stage_entry(train_dir / "images", train_dir / "masks", f"{name}_r{copy_index:02d}", image, mask)
            _stage_entry(train_dir / "images", train_dir / "masks", f"{name}_f{copy_index:02d}", *flipped)
            train_count += 2
    # 原本地种子 3 张同样过采样（×8+翻转），保持旧本地域不掉。
    for mask_path in sorted((LOCAL_SEED / "masks").glob("*.png")):
        name = mask_path.stem
        image = cv2.imdecode(np.frombuffer((LOCAL_SEED / "images" / f"{name}.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        mask = load_mask(mask_path)
        flipped = (image[:, ::-1].copy(), mask[:, ::-1].copy())
        for copy_index in range(8):
            _stage_entry(train_dir / "images", train_dir / "masks", f"local_{name}_r{copy_index:02d}", image, mask)
            _stage_entry(train_dir / "images", train_dir / "masks", f"local_{name}_f{copy_index:02d}", *flipped)
            train_count += 2
    # 各域回放抽样（含天然背景帧）。
    for label, base, limit in REPLAY_POOLS:
        for image_path in _pairs(base / "images", base / "masks", limit, rng):
            mask = load_mask(base / "masks" / f"{image_path.stem}.png")
            _stage_entry(train_dir / "images", train_dir / "masks", f"{label}_{image_path.stem}", 
                         cv2.imdecode(np.frombuffer(image_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR), mask)
            train_count += 1

    val_count = 0
    for name in RAIN_PHOTOS:
        image = cv2.imdecode(np.frombuffer((RAIN_SEED / "images" / f"{name}.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        mask = load_mask(RAIN_SEED / "masks" / f"{name}.png")
        _stage_entry(val_dir / "images", val_dir / "masks", name, image, mask)
        val_count += 1
    for label, base, limit in VAL_POOLS:
        for image_path in _pairs(base / "images", base / "masks", limit, rng):
            mask = load_mask(base / "masks" / f"{image_path.stem}.png")
            _stage_entry(val_dir / "images", val_dir / "masks", f"{label}_{image_path.stem}",
                         cv2.imdecode(np.frombuffer(image_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR), mask)
            val_count += 1

    for split, folder in (("train", train_dir), ("val", val_dir)):
        (folder / "labels").mkdir(exist_ok=True)
        lines = []
        for image_path in sorted((folder / "images").glob("*.jpg")):
            polygons = mask_to_polygons(load_mask(folder / "masks" / f"{image_path.stem}.png"))
            (folder / "labels" / f"{image_path.stem}.txt").write_text(
                "\n".join(" ".join(["0", *[f"{value:.6f}" for value in polygon]]) for polygon in polygons) + "\n",
                encoding="utf-8")
            lines.append(str(image_path.resolve()))
        (folder / f"{split}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    data_yaml = work / "data.yaml"
    data_yaml.write_text(
        f"train: {(train_dir / 'train.txt').as_posix()}\n"
        f"val: {(val_dir / 'val.txt').as_posix()}\n"
        "names:\n  0: water\n", encoding="utf-8")
    print(json.dumps({"train": train_count, "val": val_count, "data_yaml": str(data_yaml)}, ensure_ascii=False))
    return data_yaml


def train(data_yaml: Path, epochs: int, batch: int) -> Path:
    from ultralytics import YOLO

    run_dir = (ROOT / "output" / "rain_ft").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    model = YOLO(str(CHAMPION))
    started = time.perf_counter()
    model.train(data=str(data_yaml), epochs=epochs, imgsz=640, batch=batch, device=0,
                lr0=0.001, lrf=0.01, warmup_epochs=3, workers=8, cache=False, seed=20261024,
                project=str(run_dir), name="rain3_mix", exist_ok=True, deterministic=True)
    best = Path(model.trainer.best)
    print(json.dumps({"best": str(best), "elapsed_seconds": round(time.perf_counter() - started, 1),
                      "metrics": {key: float(value) for key, value in model.trainer.metrics.items()}},
                     ensure_ascii=False, indent=2))
    return best


def preview(best: Path, work: Path) -> None:
    from ultralytics import YOLO

    model = YOLO(str(best))
    out = work / "preview"
    out.mkdir(parents=True, exist_ok=True)
    targets = [(RAIN_SEED / "images" / f"{name}.png", RAIN_SEED / "masks" / f"{name}.png") for name in RAIN_PHOTOS]
    # 未参与训练的留出帧：泛化观察用（无标注，只看预测叠图）。
    targets += [(ROOT / "data" / "rain_raw" / f"{stem}.jpg", None) for stem in ("snow_006", "concat_017")]
    for image_path, mask_path in targets:
        image = cv2.imdecode(np.frombuffer(image_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        result = model.predict(image, conf=0.25, imgsz=640, verbose=False)[0]
        predicted = np.zeros(image.shape[:2], dtype=np.uint8)
        if result.masks is not None:
            for matrix in result.masks.data:
                resized = cv2.resize(matrix.cpu().numpy(), (image.shape[1], image.shape[0]))
                predicted[resized > 0.5] = 255
        overlay = image.copy()
        overlay[predicted > 0] = (30, 110, 255)
        blended = cv2.addWeighted(image, 0.55, overlay, 0.45, 0)
        record = {"image": image_path.name, "predicted_fraction": round(float((predicted > 0).mean()), 4)}
        if mask_path is not None:
            truth = load_mask(mask_path) > 0
            predicted_water = predicted > 0
            intersection = int(np.count_nonzero(predicted_water & truth))
            union = int(np.count_nonzero(predicted_water | truth))
            record["water_iou_vs_seed"] = round(intersection / union, 4) if union else 1.0
        cv2.imencode(".jpg", blended, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tofile(str(out / f"{image_path.stem}_preview.jpg"))
        print(json.dumps(record, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--skip-train", action="store_true", help="只重建数据并预览现有 best")
    args = parser.parse_args()
    work = ROOT / "data" / "rain_ft"
    work.mkdir(parents=True, exist_ok=True)
    data_yaml = build_mix(work)
    if args.skip_train:
        best = ROOT / "output" / "rain_ft" / "rain3_mix" / "weights" / "best.pt"
    else:
        best = train(data_yaml, args.epochs, args.batch)
    preview(best, ROOT / "output" / "rain_ft")


if __name__ == "__main__":
    main()
