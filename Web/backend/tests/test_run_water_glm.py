"""Unit tests for the GLM runner: whole-image PNG transport, encoding fallback, manifest."""

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


def write_jpeg(path: Path, width: int, height: int) -> Path:
    image = np.full((height, width, 3), 200, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    path.write_bytes(encoded.tobytes())
    return path


class RequestEncodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner_module()

    def test_small_image_goes_lossless_png(self):
        image = np.full((480, 640, 3), 120, dtype=np.uint8)
        data_url = self.runner._request_data_url(image)
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        decoded = cv2.imdecode(np.frombuffer(base64.b64decode(data_url.split(",", 1)[1]), dtype=np.uint8),
                               cv2.IMREAD_COLOR)
        self.assertEqual((decoded.shape[1], decoded.shape[0]), (640, 480))

    def test_oversize_png_falls_back_to_jpeg(self):
        image = np.random.default_rng(7).integers(0, 256, (1080, 1920, 3), dtype=np.uint8)
        with patch.object(self.runner, "REQUEST_MAX_BYTES", 1024):
            data_url = self.runner._request_data_url(image)
        # 噪声图 PNG 必然超限：应回退 JPEG，且降采样后不超过兜底边长。
        self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))
        decoded = cv2.imdecode(np.frombuffer(base64.b64decode(data_url.split(",", 1)[1]), dtype=np.uint8),
                               cv2.IMREAD_COLOR)
        self.assertLessEqual(max(decoded.shape[:2]), self.runner.FALLBACK_MAX_SIDE)


class ProcessWholeImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner_module()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output_dir = Path(self.temp.name) / "outputs"
        self.frame_path = write_jpeg(Path(self.temp.name) / "frame.jpg", 640, 480)

    @staticmethod
    def _fake_model(detections, calls):
        def fake(image, url, api_key, model):
            calls.append((image.shape[1], image.shape[0]))
            return {"detections": detections}
        return fake

    def test_whole_image_sent_regardless_of_mask_entry(self):
        # manifest 的 mask 字段当前不参与送检：带不带掩膜都应整图送检。
        calls: list[tuple[int, int]] = []
        detections = [{"label": "puddle", "confidence": 0.8,
                       "polygon": [[0.0, 0.0], [0.99, 0.0], [0.99, 0.99], [0.0, 0.99]]}]
        with patch.object(self.runner, "_call_model", self._fake_model(detections, calls)):
            record = self.runner._process(
                {"path": self.frame_path, "mask": Path(self.temp.name) / "missing.png"},
                self.output_dir, "https://example.invalid", "key", "glm-test")
        self.assertEqual(calls, [(640, 480)])
        self.assertEqual(record["request_encoding"], "png-lossless")
        self.assertEqual(record["instance_count"], 1)
        self.assertGreater(record["suspected_pixels"], 0)
        self.assertNotIn("regions", record)

    def test_retry_then_runtime_error_when_model_never_returns_json(self):
        attempts: list[int] = []

        def failing(image, url, api_key, model):
            attempts.append(1)
            raise ValueError("模型没有返回可读取的分割结果文本。")

        with patch.object(self.runner, "_call_model", failing):
            with self.assertRaises(RuntimeError):
                self.runner._process(
                    {"path": self.frame_path, "mask": None},
                    self.output_dir, "https://example.invalid", "key", "glm-test")
        self.assertEqual(len(attempts), 2)


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
