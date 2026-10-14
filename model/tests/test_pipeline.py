from __future__ import annotations

import csv
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from waterwatch.flood import shoreline_change
from waterwatch.images import (
    homography_from_file,
    load_scene,
    read_mask,
    read_rgb,
    read_thermal,
    save_png,
)
from waterwatch.learning import evaluate_model, load_model, save_prediction, train_model
from waterwatch.manifest import read_manifest
from waterwatch.plan import read_sites, regional_plan
from waterwatch.annotations import labelme_to_mask


def _write_rgb(path: Path, rgb: np.ndarray) -> None:
    save_png(path, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))


def _metadata(case_id: str, site_id: str, captured_at: str) -> dict:
    return {
        "case_id": case_id,
        "site_id": site_id,
        "captured_at": captured_at,
        "scene_type": "river",
        "rgb_device": "test camera",
    }


def test_ponding_end_to_end_with_held_out_scene(tmp_path: Path) -> None:
    entries = []
    for number, split in enumerate(("train", "train", "test")):
        rgb = np.zeros((66, 66, 3), dtype=np.uint8)
        mask = np.zeros((66, 66), dtype=np.uint8)
        thermal = np.zeros((66, 66), dtype=np.float32)
        # Each class occupies a broad region; wet road is distinct from standing water.
        colors = ((130, 100, 70), (70, 70, 70), (45, 75, 125))
        heat = (300, 230, 150)
        for label in range(3):
            start, end = label * 22, (label + 1) * 22
            rgb[:, start:end] = np.array(colors[label], np.uint8) + number
            mask[:, start:end] = label
            thermal[:, start:end] = heat[label] + number
        case_id = f"p{number}"
        _write_rgb(tmp_path / f"{case_id}.png", rgb)
        thermal[:5, :] = np.nan  # Real separate sensors often leave part of RGB uncovered.
        np.save(tmp_path / f"{case_id}_thermal.npy", thermal)
        save_png(tmp_path / f"{case_id}_mask.png", mask)
        entries.append({
            "case_id": case_id,
            "site_id": f"site_{number}",
            "task": "ponding",
            "split": split,
            "captured_at": f"2026-10-{14 + number}T10:00:00+08:00",
            "rgb": f"{case_id}.png",
            "thermal": f"{case_id}_thermal.npy",
            "thermal_registration": "aligned",
            "mask": f"{case_id}_mask.png",
        })
    manifest_path = tmp_path / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(entries[0]))
        writer.writeheader()
        writer.writerows(entries)
    cases = read_manifest(manifest_path)
    rgb_model_path = tmp_path / "ponding_rgb.joblib"
    train_model(cases, "ponding", "rgb", rgb_model_path, max_edge=128, max_per_class=500, paired_support=True)
    rgb_results = evaluate_model(cases, load_model(rgb_model_path), split="test")
    model_path = tmp_path / "ponding_fusion.joblib"
    train_model(cases, "ponding", "fusion", model_path, max_edge=128, max_per_class=500)
    bundle = load_model(model_path)
    results = evaluate_model(cases, bundle, split="test")
    assert rgb_results["total_evaluated_pixels"] == results["total_evaluated_pixels"] == 61 * 66
    assert rgb_results["train_case_ids"] == results["train_case_ids"]
    assert results["metrics"]["classes"]["suspected_ponding"]["recall"] > 0.9
    assert results["metrics"]["wet_road_false_ponding_rate"] < 0.1
    test_case = cases[-1]
    scene = load_scene(test_case.rgb, thermal_path=test_case.thermal, thermal_registration="aligned")
    roi = np.ones((66, 66), np.uint8)
    roi[:10, :] = 0
    report = save_prediction(tmp_path / "prediction", bundle, scene, _metadata("p2", "site_2", test_case.captured_at), roi)
    assert report["suspected_pixels"] > 0
    assert report["roi_excluded_pixels"] == 10 * 66
    assert set(np.unique(read_mask(tmp_path / "prediction" / "classes.png"))) == {0, 1, 2, 255}


def test_independent_thermal_requires_explicit_alignment(tmp_path: Path) -> None:
    rgb = np.full((32, 32, 3), 100, dtype=np.uint8)
    thermal = np.full((32, 32), 210, dtype=np.uint16)
    _write_rgb(tmp_path / "rgb.png", rgb)
    save_png(tmp_path / "thermal.png", thermal)
    with pytest.raises(ValueError, match="配准"):
        load_scene(tmp_path / "rgb.png", thermal_path=tmp_path / "thermal.png")
    scene = load_scene(tmp_path / "rgb.png", thermal_path=tmp_path / "thermal.png", thermal_registration="aligned")
    assert scene.thermal is not None and scene.thermal.shape == (32, 32)
    _write_rgb(tmp_path / "false_thermal.png", rgb)
    with pytest.raises(ValueError, match="二维单通道"):
        read_thermal(tmp_path / "false_thermal.png")


def test_control_point_registration_and_shoreline_change(tmp_path: Path) -> None:
    controls = {
        "source_points": [[2, 2], [29, 2], [29, 29], [2, 29]],
        "target_points": [[0, 2], [27, 2], [27, 29], [0, 29]],
    }
    registration = tmp_path / "after_to_before.json"
    registration.write_text(json.dumps(controls), encoding="utf-8")
    matrix, quality = homography_from_file(registration)
    assert quality["inliers"] == 4
    assert abs(matrix[0, 2] + 2) < 1e-4
    before_rgb = np.full((32, 32, 3), 100, np.uint8)
    after_rgb = before_rgb.copy()
    before_mask = np.zeros((32, 32), np.uint8)
    after_mask = np.zeros((32, 32), np.uint8)
    before_mask[:, :10] = 1
    after_mask[:, :17] = 1  # With after-to-before shift -2, water grows to x=14.
    _write_rgb(tmp_path / "before.png", before_rgb)
    _write_rgb(tmp_path / "after.png", after_rgb)
    save_png(tmp_path / "before_mask.png", before_mask)
    save_png(tmp_path / "after_mask.png", after_mask)
    report = shoreline_change(
        tmp_path / "before.png", tmp_path / "before_mask.png",
        tmp_path / "after.png", tmp_path / "after_mask.png",
        _metadata("before", "river_a", "2026-10-14T10:00:00+08:00"),
        _metadata("after", "river_a", "2026-10-15T10:00:00+08:00"),
        tmp_path / "change", registration=registration,
        before_mask_origin="manual", after_mask_origin="manual",
    )
    assert report["newly_water_pixels"] > 0
    assert report["registration_quality"]["inliers"] == 4
    assert report["mask_origins"] == {"before": "manual", "after": "manual"}
    assert report["input_sources"]["before_rgb"]["sha256"]
    assert (tmp_path / "change" / "change_overlay.png").is_file()


def test_shoreline_refuses_unregistered_or_mismatched_sites(tmp_path: Path) -> None:
    rgb = np.full((32, 32, 3), 100, np.uint8)
    mask = np.zeros((32, 32), np.uint8)
    _write_rgb(tmp_path / "rgb.png", rgb)
    save_png(tmp_path / "mask.png", mask)
    common = (
        tmp_path / "rgb.png", tmp_path / "mask.png",
        tmp_path / "rgb.png", tmp_path / "mask.png",
    )
    before = _metadata("before", "a", "2026-10-14T10:00:00+08:00")
    after = _metadata("after", "b", "2026-10-15T10:00:00+08:00")
    with pytest.raises(ValueError, match="site_id"):
        shoreline_change(*common, before, after, tmp_path / "out", same_view=True)
    after["site_id"] = "a"
    with pytest.raises(ValueError, match="--registration"):
        shoreline_change(*common, before, after, tmp_path / "out")


def test_regional_plan_does_not_infer_point_level_hazard(tmp_path: Path) -> None:
    sites_path = tmp_path / "sites.csv"
    sites_path.write_text(
        "site_id,region,task,notes,latitude,longitude\n"
        "a,常州市,ponding,low street,,\n"
        "b,常州市,shoreline,canal bank,,\n"
        "c,南京市,shoreline,other city,,\n",
        encoding="utf-8",
    )
    event = {"region": "常州市", "source_url": "https://example.org", "issued_at": "2026-10-14T10:00:00+08:00"}
    result = regional_plan(event, read_sites(sites_path), "shoreline")
    assert result["candidate_count"] == 1
    assert result["candidate_sites"][0]["site_id"] == "b"
    assert "待影像与人员核查" in result["candidate_sites"][0]["reason"]


def test_labelme_polygon_conversion_preserves_three_classes(tmp_path: Path) -> None:
    rgb = np.full((40, 40, 3), 100, np.uint8)
    rgb_path = tmp_path / "rgb.png"
    _write_rgb(rgb_path, rgb)
    shapes = []
    for label, left, right in (("dry", 0, 12), ("wet", 13, 25), ("ponding", 26, 39)):
        shapes.append({"label": label, "shape_type": "polygon", "points": [[left, 0], [right, 0], [right, 39], [left, 39]]})
    annotation = tmp_path / "labelme.json"
    annotation.write_text(json.dumps({"imageWidth": 40, "imageHeight": 40, "shapes": shapes}), encoding="utf-8")
    output = tmp_path / "mask.png"
    labelme_to_mask(annotation, rgb_path, "ponding", output)
    assert set(np.unique(read_mask(output))) == {0, 1, 2}
