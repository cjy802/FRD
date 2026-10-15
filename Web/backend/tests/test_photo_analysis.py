"""End-to-end tests for the post-flight photo analysis endpoints using a stub runner."""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from photo_analysis import PhotoAnalysisManager

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

STUB_RUNNER = r'''
import base64, json, sys
from pathlib import Path

arguments = dict(zip(sys.argv[1::2], sys.argv[2::2]))
output_dir = Path(arguments["--output-dir"])
output_dir.mkdir(parents=True, exist_ok=True)
inputs = sorted(path for path in Path(arguments["--input-dir"]).iterdir() if path.suffix.lower() in {".png", ".jpg", ".jpeg"})
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
images = []
for path in inputs:
    stem = path.stem
    (output_dir / f"{stem}__overlay.png").write_bytes(png)
    (output_dir / f"{stem}__suspected.png").write_bytes(png)
    (output_dir / f"{stem}__confidence.png").write_bytes(png)
    images.append({"name": path.name, "sha256": "0" * 64, "width": 1, "height": 1,
                   "model_input_size": 256, "suspected_pixels": 1, "suspected_fraction": 1.0,
                   "suspected_patch_count": 1, "mean_confidence_on_suspected": 0.9,
                   "overlay": f"{stem}__overlay.png", "suspected_mask": f"{stem}__suspected.png",
                   "confidence_map": f"{stem}__confidence.png"})
payload = {"model": {"default_model": "stub.onnx", "task": "ponding", "water_class": 1,
                     "input_size": 256, "normalize": "div255", "model_sha256": "0" * 64},
           "images": images, "elapsed_seconds": 0.01, "interpretation": "stub"}
(output_dir / "results.json").write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps(payload))
'''


class PhotoAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        model_dir = base / "models"
        model_dir.mkdir()
        (model_dir / "registry.json").write_text(json.dumps({
            "default_model": "stub.onnx", "task": "ponding", "water_class": 1,
            "input_size": 256, "normalize": "div255",
        }), encoding="utf-8")
        (model_dir / "stub.onnx").write_bytes(b"stub")
        runner = base / "stub_runner.py"
        runner.write_text(STUB_RUNNER, encoding="utf-8")
        self.manager = PhotoAnalysisManager(
            storage_dir=base / "storage",
            python_path=Path(sys.executable),
            model_dir=model_dir,
            runner_path=runner,
        )
        self.patch = patch.object(main, "photo_analysis", self.manager)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = TestClient(main.app, base_url="http://127.0.0.1:5173", client=("127.0.0.1", 50001))
        self.headers = {"Origin": "http://127.0.0.1:5173"}

    def _wait_done(self, job_id, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.client.get(f"/api/photo-analysis/jobs/{job_id}", headers=self.headers).json()
            if job["state"] in ("done", "error"):
                return job
            time.sleep(0.1)
        self.fail("job did not finish in time")

    def test_health_reports_ready(self):
        response = self.client.get("/api/photo-analysis/health", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["available"])
        self.assertFalse(payload["busy"])

    def test_health_unavailable_without_model(self):
        broken = PhotoAnalysisManager(
            storage_dir=self.manager.storage_dir,
            python_path=Path(sys.executable),
            model_dir=Path(self.temp.name) / "missing",
            runner_path=self.manager.runner_path,
        )
        with patch.object(main, "photo_analysis", broken):
            response = self.client.get("/api/photo-analysis/health", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["available"])

    def test_submit_run_and_fetch_outputs(self):
        submit = self.client.post(
            "/api/photo-analysis/jobs",
            headers=self.headers,
            files=[("photos", ("pond.jpg", TINY_PNG, "image/png")), ("photos", ("pond2.jpg", TINY_PNG, "image/png"))],
            data={"site_name": "s1", "note": "n"},
        )
        self.assertEqual(submit.status_code, 202, submit.text)
        job_id = submit.json()["job_id"]
        job = self._wait_done(job_id)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(len(job["results"]["images"]), 2)
        self.assertEqual(job["context"]["site_name"], "s1")
        overlay = job["results"]["images"][0]["overlay"]
        fetched = self.client.get(f"/api/photo-analysis/jobs/{job_id}/files/{overlay}", headers=self.headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.content, TINY_PNG)
        original = self.client.get(f"/api/photo-analysis/jobs/{job_id}/files/inputs/000-pond.jpg", headers=self.headers)
        self.assertEqual(original.status_code, 200)

    def test_rejects_unsupported_suffix(self):
        submit = self.client.post(
            "/api/photo-analysis/jobs",
            headers=self.headers,
            files=[("photos", ("notes.txt", b"hello", "text/plain"))],
        )
        self.assertEqual(submit.status_code, 400)

    def test_rejects_unknown_file_name(self):
        submit = self.client.post(
            "/api/photo-analysis/jobs",
            headers=self.headers,
            files=[("photos", ("pond.jpg", TINY_PNG, "image/png"))],
        )
        job_id = submit.json()["job_id"]
        self._wait_done(job_id)
        evil = self.client.get(f"/api/photo-analysis/jobs/{job_id}/files/..%2F..%2Fregistry", headers=self.headers)
        self.assertIn(evil.status_code, (404, 400))

    def test_local_origin_guard(self):
        response = self.client.post(
            "/api/photo-analysis/jobs",
            headers={"Origin": "https://evil.example"},
            files=[("photos", ("pond.jpg", TINY_PNG, "image/png"))],
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/api/photo-analysis/jobs",
            files=[("photos", ("pond.jpg", TINY_PNG, "image/png"))],
        )
        self.assertEqual(response.status_code, 403)

    def test_unknown_job_returns_404(self):
        response = self.client.get("/api/photo-analysis/jobs/nope", headers=self.headers)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
