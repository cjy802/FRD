"""Unit tests for the GLM runner's crop-based recheck (regions, mapping, fallback)."""

from __future__ import annotations

import base64
import importlib.util
import json
import tempfile
import argparse
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8QAAwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def load_runner_module():
    module_name = "run_water_glm_test_target"
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "run_water_glm.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_jpeg(path: Path, width: int, height: int) -> np.ndarray:
    image = np.full((height, width, 3), 200, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    path.write_bytes(encoded.tobytes())
    return image


def write_mask(path: Path, width: int, height: int, boxes: list[tuple[int, int, int, int]]) -> Path:
    mask = np.zeros((height, width), dtype=np.uint8)
    for x, y, w, h in boxes:
        mask[y:y + h, x:x + w] = 255
    ok, encoded = cv2.imencode(".png", mask)
    assert ok
    path.write_bytes(encoded.tobytes())
    return path


class SuspectedRegionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner_module()

    def test_region_expands_with_minimum_side_and_stays_inside(self):
        mask = np.zeros((480, 640), dtype=np.uint8)
        mask[200:260, 100:180] = 255
        regions = self.runner._suspected_regions(mask, 640, 480)
        self.assertEqual(len(regions), 1)
        x0, y0, x1, y1 = regions[0]
        # 80x60 外扩 1.5 倍不足 256 最短边，应补齐到 256 见方并夹在图内。
        self.assertGreaterEqual(x1 - x0, self.runner.REGION_MIN_SIDE - 2)
        self.assertGreaterEqual(y1 - y0, self.runner.REGION_MIN_SIDE - 2)
        self.assertTrue(x0 <= 140 < x1)
        self.assertTrue(y0 <= 230 < y1)
        self.assertGreaterEqual(x0, 0)
        self.assertGreaterEqual(y0, 0)
        self.assertLessEqual(x1, 640)
        self.assertLessEqual(y1, 480)

    def test_nearby_components_merge_into_one_region(self):
        mask = np.zeros((600, 800), dtype=np.uint8)
        mask[100:140, 100:140] = 255
        mask[110:150, 180:220] = 255
        regions = self.runner._suspected_regions(mask, 800, 600)
        self.assertEqual(len(regions), 1)

    def test_region_count_capped_without_losing_candidates(self):
        boxes = [(60, 60, 20, 20), (700, 60, 20, 20), (1340, 60, 20, 20),
                 (60, 700, 20, 20), (700, 700, 20, 20), (1340, 700, 20, 20)]
        mask = np.zeros((900, 1600), dtype=np.uint8)
        for x, y, w, h in boxes:
            mask[y:y + h, x:x + w] = 255
        regions = self.runner._suspected_regions(mask, 1600, 900)
        self.assertLessEqual(len(regions), self.runner.REGION_MAX_COUNT)
        for x, y, _, _ in boxes:
            covered = any(x0 <= x + 10 <= x1 and y0 <= y + 10 <= y1
                          for x0, y0, x1, y1 in regions)
            self.assertTrue(covered, f"候选区域 ({x},{y}) 没有被任何裁块覆盖")

    def test_empty_mask_yields_no_regions(self):
        mask = np.zeros((480, 640), dtype=np.uint8)
        self.assertEqual(self.runner._suspected_regions(mask, 640, 480), [])


class ProcessRecheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner_module()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output_dir = Path(self.temp.name) / "outputs"
        self.frame_path = Path(self.temp.name) / "frame.jpg"
        write_jpeg(self.frame_path, 640, 480)

    @staticmethod
    def _fake_model(detections, calls):
        def fake(image, url, api_key, model, cropped=False):
            calls.append({"size": (image.shape[1], image.shape[0]), "cropped": cropped})
            return {"detections": detections}
        return fake

    def test_cropped_recheck_maps_polygons_back_to_full_frame(self):
        mask_path = write_mask(Path(self.temp.name) / "frame__suspected.png", 640, 480,
                               [(100, 200, 80, 60)])
        calls: list[dict] = []
        # 多边形几乎铺满裁块：映射回全帧后必须仍然落在裁块范围内。
        detections = [{"label": "puddle", "confidence": 0.8,
                       "polygon": [[0.0, 0.0], [0.99, 0.0], [0.99, 0.99], [0.0, 0.99]]}]
        with patch.object(self.runner, "_call_model", self._fake_model(detections, calls)):
            record = self.runner._process(
                {"path": self.frame_path, "mask": mask_path},
                self.output_dir, "https://example.invalid", "key", "glm-test")

        self.assertEqual(record["recheck_mode"], "cropped")
        self.assertEqual(len(record["regions"]), 1)
        x0, y0, x1, y1 = record["regions"][0]
        self.assertEqual(calls[0]["cropped"], True)
        self.assertEqual(calls[0]["size"], (x1 - x0, y1 - y0))
        self.assertEqual(record["instance_count"], 1)
        self.assertGreater(record["suspected_pixels"], 0)
        # 全帧掩膜里的疑似像素必须全部位于送检裁块内部。
        suspect = cv2.imdecode(
            np.frombuffer((self.output_dir / record["suspected_mask"]).read_bytes(), dtype=np.uint8),
            cv2.IMREAD_GRAYSCALE)
        ys, xs = np.nonzero(suspect)
        self.assertTrue(np.all(xs >= x0) and np.all(xs < x1))
        self.assertTrue(np.all(ys >= y0) and np.all(ys < y1))

    def test_missing_mask_falls_back_to_whole_frame(self):
        calls: list[dict] = []
        with patch.object(self.runner, "_call_model", self._fake_model([], calls)):
            record = self.runner._process(
                {"path": self.frame_path, "mask": None},
                self.output_dir, "https://example.invalid", "key", "glm-test")
        self.assertEqual(record["recheck_mode"], "full")
        self.assertEqual(record["regions"], [[0, 0, 640, 480]])
        self.assertEqual(calls[0]["cropped"], False)
        self.assertEqual(calls[0]["size"], (640, 480))
        self.assertEqual(record["instance_count"], 0)

    def test_all_regions_failing_raises_for_queue_retry(self):
        mask_path = write_mask(Path(self.temp.name) / "frame__suspected.png", 640, 480,
                               [(100, 200, 80, 60)])

        def failing(image, url, api_key, model, cropped=False):
            raise ValueError("模型没有返回可读取的分割结果文本。")

        with patch.object(self.runner, "_call_model", failing):
            with self.assertRaises(ValueError):
                self.runner._process(
                    {"path": self.frame_path, "mask": mask_path},
                    self.output_dir, "https://example.invalid", "key", "glm-test")


class ManifestEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner_module()

    def _args(self, manifest: Path | None, input_dir: Path | None) -> argparse.Namespace:
        return argparse.Namespace(manifest=manifest, input_dir=input_dir)

    def test_collect_accepts_strings_dicts_and_mixed(self):
        manifest = Path(self.temp_dir()) / "manifest.json"
        manifest.write_text(json.dumps([
            "C:/frames/a.jpg",
            {"path": "C:/frames/b.jpg", "mask": "C:/jobs/b__suspected.png"},
            {"path": "C:/frames/c.jpg"},
        ], ensure_ascii=False), encoding="utf-8")
        entries = self.runner._collect_images(self._args(manifest, None))
        self.assertEqual([entry["path"].name for entry in entries], ["a.jpg", "b.jpg", "c.jpg"])
        self.assertIsNone(entries[0]["mask"])
        self.assertEqual(entries[1]["mask"].name, "b__suspected.png")
        self.assertIsNone(entries[2]["mask"])

    def test_collect_rejects_unknown_entry_shape(self):
        manifest = Path(self.temp_dir()) / "manifest.json"
        manifest.write_text(json.dumps([{"file": "C:/frames/a.jpg"}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.runner._collect_images(self._args(manifest, None))

    def temp_dir(self) -> str:
        if not hasattr(self, "_temp"):
            self._temp = tempfile.TemporaryDirectory()
            self.addCleanup(self._temp.cleanup)
        return self._temp.name


if __name__ == "__main__":
    unittest.main()
