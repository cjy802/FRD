"""End-to-end tests for the post-flight photo analysis endpoints using stub runners."""

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
import photo_analysis as photo_analysis_module
from photo_analysis import ONNX_MODEL_ID, PhotoAnalysisManager
from task_store import TaskStore
from auth_test_support import login_admin

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8QAAwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

LEGACY_STUB_RUNNER = r'''
import base64, json, sys
from pathlib import Path

arguments = dict(zip(sys.argv[1::2], sys.argv[2::2]))
output_dir = Path(arguments["--output-dir"])
output_dir.mkdir(parents=True, exist_ok=True)
inputs = sorted(path for path in Path(arguments["--input-dir"]).iterdir() if path.suffix.lower() in {".png", ".jpg", ".jpeg"})
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8QAAwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
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

# 队列 stub：按帧内容决定行为 —— FAIL 永远失败；FLAKY 第一次失败、重试成功；
# SLOW 睡 2 秒（用于取消/超时）；CLEAN 成功但零疑似像素（用于自动删帧）；
# 其余直接成功且带疑似像素。逐帧追加 results.jsonl。
QUEUE_STUB_RUNNER = r'''
import base64, json, sys, time
from pathlib import Path

arguments = dict(zip(sys.argv[1::2], sys.argv[2::2]))
output_dir = Path(arguments["--output-dir"])
output_dir.mkdir(parents=True, exist_ok=True)
paths = json.loads(Path(arguments["--manifest"]).read_text(encoding="utf-8"))
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8QAAwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
seen_log = output_dir / "stub-seen.txt"
with (output_dir / "results.jsonl").open("a", encoding="utf-8") as jsonl:
    for raw in paths:
        path = Path(raw)
        stem = path.stem
        data = path.read_bytes()
        record = None
        if data.startswith(b"FAIL"):
            record = {"name": path.name, "error": "stub: persistent failure"}
        else:
            if data.startswith(b"FLAKY"):
                seen = seen_log.read_text(encoding="utf-8") if seen_log.is_file() else ""
                with seen_log.open("a", encoding="utf-8") as log:
                    log.write(path.name + "\n")
                if seen.count(path.name) < 1:
                    record = {"name": path.name, "error": "stub: transient failure"}
            if record is None:
                if data.startswith(b"SLOW"):
                    time.sleep(2.0)
                suspected = 0 if data.startswith(b"CLEAN") else 1
                (output_dir / f"{stem}__overlay.png").write_bytes(png)
                (output_dir / f"{stem}__suspected.png").write_bytes(png)
                (output_dir / f"{stem}__confidence.png").write_bytes(png)
                record = {"name": path.name, "sha256": "0" * 64, "width": 1, "height": 1,
                          "model_input_size": 256, "suspected_pixels": suspected,
                          "suspected_fraction": float(suspected),
                          "suspected_patch_count": suspected, "patch_area_pixels": [suspected],
                          "mean_confidence_on_suspected": 0.9 if suspected else 0.0,
                          "overlay": f"{stem}__overlay.png", "suspected_mask": f"{stem}__suspected.png",
                          "confidence_map": f"{stem}__confidence.png"}
        jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        jsonl.flush()
payload = {"model": {"default_model": "stubq", "engine": "stub", "task": "ponding", "water_class": 1,
                     "input_size": 256, "normalize": "div255", "model_sha256": "0" * 64},
           "images": [], "elapsed_seconds": 0.01, "interpretation": "stub-queue"}
(output_dir / "results.json").write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps(payload))
'''


def _make_stub_manager(base: Path, runner_text: str) -> PhotoAnalysisManager:
    model_dir = base / "models"
    model_dir.mkdir(exist_ok=True)
    (model_dir / "registry.json").write_text(json.dumps({
        "default_model": "stub.onnx", "task": "ponding", "water_class": 1,
        "input_size": 256, "normalize": "div255",
    }), encoding="utf-8")
    (model_dir / "stub.onnx").write_bytes(b"stub")
    runner = base / "stub_runner.py"
    runner.write_text(runner_text, encoding="utf-8")
    return PhotoAnalysisManager(
        storage_dir=base / "storage",
        python_path=Path(sys.executable),
        model_dir=model_dir,
        runner_path=runner,
        tasks_root=base / "tasks",
    )


class PhotoAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.manager = _make_stub_manager(base, LEGACY_STUB_RUNNER)
        self.patch = patch.object(main, "photo_analysis", self.manager)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = TestClient(main.app, base_url="http://127.0.0.1:5173", client=("127.0.0.1", 50001))
        login_admin(self.client)
        self.headers = {"Origin": "http://127.0.0.1:5173"}

    def _wait_done(self, job_id, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.manager.job(job_id)
            if job["state"] in ("done", "error", "cancelled"):
                return job
            time.sleep(0.05)
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
            yolo_python_path=Path(self.temp.name) / "missing-env" / "python.exe",
        )
        # 屏蔽本机真实 .env / conda 环境，保证判定只取决于传入的缺失路径。
        with patch.object(main, "photo_analysis", broken), \
                patch.object(photo_analysis_module, "_configured_value", lambda name, default="": ""):
            response = self.client.get("/api/photo-analysis/health", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["available"])

    def test_legacy_batch_flow_still_works(self):
        job_id = self.manager.submit(
            [("pond.jpg", TINY_PNG), ("pond2.jpg", TINY_PNG)],
            context={"site_name": "s1", "note": "n", "frames": [{"altitude_m": 50}, {"altitude_m": 50}]},
        )
        job = self._wait_done(job_id)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(len(job["results"]["images"]), 2)
        self.assertEqual(job["context"]["site_name"], "s1")
        self.assertEqual(job["results"]["images"][0]["altitude_m"], 50.0)
        overlay = job["results"]["images"][0]["overlay"]
        fetched = self.client.get(f"/api/photo-analysis/jobs/{job_id}/files/{overlay}", headers=self.headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.content, TINY_PNG)
        original = self.client.get(f"/api/photo-analysis/jobs/{job_id}/files/inputs/000-pond.jpg", headers=self.headers)
        self.assertEqual(original.status_code, 200)

    def test_legacy_rejects_unsupported_suffix(self):
        from photo_analysis import PhotoAnalysisError
        with self.assertRaises(PhotoAnalysisError):
            self.manager.submit([("notes.txt", b"hello")])

    def test_local_origin_guard(self):
        response = self.client.post(
            "/api/photo-analysis/jobs",
            headers={"Origin": "https://evil.example"},
            json={"task_id": "x", "frame_ids": []},
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/photo-analysis/jobs", json={"task_id": "x", "frame_ids": []})
        self.assertEqual(response.status_code, 403)

    def test_unknown_job_returns_404(self):
        response = self.client.get("/api/photo-analysis/jobs/nope", headers=self.headers)
        self.assertEqual(response.status_code, 404)


class QueuePhotoAnalysisTests(unittest.TestCase):
    """Frame-queue flow: streaming append, retries, circuit breaker, cancel, recovery."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.manager = _make_stub_manager(base, QUEUE_STUB_RUNNER)
        self.store = TaskStore(storage_dir=base / "tasks")
        self.manager.frame_resolver = self._resolve_frame
        self.manager_patch = patch.object(main, "photo_analysis", self.manager)
        self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)
        self.tasks_patch = patch.object(main, "tasks", self.store)
        self.tasks_patch.start()
        self.addCleanup(self.tasks_patch.stop)
        self.client = TestClient(main.app, base_url="http://127.0.0.1:5173", client=("127.0.0.1", 50001))
        login_admin(self.client)
        self.headers = {"Origin": "http://127.0.0.1:5173"}
        self.task = self.store.create({"anchors": []}, "2026-10-23T08:00:00+08:00")

    def _resolve_frame(self, task_id: str, frame_id: str) -> dict:
        frame = next((item for item in self.store.frames(task_id) if item.get("frame_id") == frame_id), None)
        if frame is None:
            raise KeyError(frame_id)
        meta = {key: frame.get(key) for key in
                ("time_seconds", "latitude", "longitude", "altitude_m", "gimbal_pitch_deg", "name")}
        return {"name": frame["file_name"],
                "path": self.store.frame_directory(task_id) / frame["file_name"],
                "meta": meta}

    def _add_frame(self, payload: bytes = TINY_PNG, altitude: float | None = 60.0) -> str:
        frame = self.store.add_frame(self.task["task_id"], "frame.jpg", payload, {
            "time_seconds": 1.0, "latitude": 31.6, "longitude": 119.9,
            "altitude_m": altitude, "gimbal_pitch_deg": -90.0,
        })
        return frame["frame_id"]

    def _wait_state(self, job_id, states=("done", "error", "cancelled"), timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.manager.job(job_id)
            if job["state"] in states:
                return job
            time.sleep(0.05)
        self.fail(f"job did not reach {states} in time")

    def _fast_retries(self):
        patcher = patch.object(photo_analysis_module, "QUEUE_RETRY_DELAYS", (0.05, 0.05))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_remove_all_frames_clears_files_index_and_api(self):
        task_id = self.task["task_id"]
        for index in range(3):
            self.store.add_frame(task_id, f"frame-{index}.jpg", TINY_PNG, {
                "time_seconds": float(index), "latitude": 31.6, "longitude": 119.9,
            })
        directory = self.store.frame_directory(task_id)
        self.assertEqual(len(self.store.frames(task_id)), 3)
        self.assertEqual(len(list(directory.glob("*.jpg"))), 3)

        response = self.client.delete(f"/api/tasks/{task_id}/frames", headers=self.headers)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.store.frames(task_id), [])
        self.assertEqual(list(directory.glob("*.jpg")), [])
        listing = self.client.get(f"/api/tasks/{task_id}/frames", headers=self.headers)
        self.assertEqual(listing.json()["frames"], [])

    def test_queue_submit_completes_with_enrichment(self):
        first = self._add_frame()
        second = self._add_frame()
        submit = self.client.post("/api/photo-analysis/jobs", headers=self.headers, json={
            "task_id": self.task["task_id"], "frame_ids": [first, second], "camera_fov_deg": 75.0,
            "recheck_of_job_id": "20260101000000-firstjob"})
        self.assertEqual(submit.status_code, 202, submit.text)
        job_id = submit.json()["job_id"]
        job = self._wait_state(job_id)
        self.assertEqual(job["state"], "done", job.get("error"))
        # 复检标记随 context 回显，前端据此切到“双模型确认”口径。
        self.assertEqual(job["context"].get("recheck_of_job_id"), "20260101000000-firstjob")
        self.assertEqual(job["counts"], {"total": 2, "done": 2, "failed": 0, "pending": 0, "running": 0, "cancelled": 0})
        self.assertEqual(len(job["results"]["images"]), 2)
        image = job["results"]["images"][0]
        self.assertEqual(image["altitude_m"], 60.0)
        self.assertEqual(image["area_estimate"], "nadir")
        self.assertGreater(image["estimated_area_m2"], 0)
        overlay = image["overlay"]
        fetched = self.client.get(f"/api/photo-analysis/jobs/{job_id}/files/{overlay}", headers=self.headers)
        self.assertEqual(fetched.status_code, 200)
        task_record = self.store.get(self.task["task_id"])
        self.assertIn(job_id, task_record["analysis_job_ids"])

    def test_finished_job_is_served_after_backend_restart(self):
        frame_id = self._add_frame()
        submit = self.client.post("/api/photo-analysis/jobs", headers=self.headers, json={
            "task_id": self.task["task_id"], "frame_ids": [frame_id]})
        self.assertEqual(submit.status_code, 202, submit.text)
        job_id = submit.json()["job_id"]
        job = self._wait_state(job_id)
        file_name = job["items"][0]["file_name"]
        # 模拟后端重启（含 --reload）：同一磁盘、全新内存的 manager。
        restarted = _make_stub_manager(Path(self.temp.name), QUEUE_STUB_RUNNER)
        restarted.frame_resolver = self._resolve_frame
        with patch.object(main, "photo_analysis", restarted):
            fetched = self.client.get(f"/api/photo-analysis/jobs/{job_id}", headers=self.headers)
            self.assertEqual(fetched.status_code, 200, fetched.text)
            payload = fetched.json()
            self.assertEqual(payload["state"], "done")
            self.assertEqual(payload["counts"]["done"], 1)
            self.assertEqual(len(payload["results"]["images"]), 1)
            original = self.client.get(
                f"/api/photo-analysis/jobs/{job_id}/files/inputs/{file_name}", headers=self.headers)
            self.assertEqual(original.status_code, 200)
            overlay = payload["results"]["images"][0]["overlay"]
            overlaid = self.client.get(
                f"/api/photo-analysis/jobs/{job_id}/files/{overlay}", headers=self.headers)
            self.assertEqual(overlaid.status_code, 200)

    def test_queue_streaming_append_and_close(self):
        submit = self.client.post("/api/photo-analysis/jobs", headers=self.headers, json={
            "task_id": self.task["task_id"], "frame_ids": [], "streaming": True})
        self.assertEqual(submit.status_code, 202, submit.text)
        job_id = submit.json()["job_id"]
        first = self._add_frame()
        appended = self.client.post(f"/api/photo-analysis/jobs/{job_id}/frames",
                                    headers=self.headers, json={"frame_ids": [first]})
        self.assertEqual(appended.status_code, 200)
        self.assertEqual(appended.json()["appended"], 1)
        # 第一帧识别完成后任务仍在运行，可以继续追加（截取即入队的关键行为）。
        deadline = time.time() + 10
        while time.time() < deadline:
            snapshot = self.manager.job(job_id)
            if snapshot["counts"]["done"] >= 1 and snapshot["state"] == "running":
                break
            time.sleep(0.05)
        else:
            self.fail("first appended frame did not finish while job stayed open")
        second = self._add_frame()
        appended = self.client.post(f"/api/photo-analysis/jobs/{job_id}/frames",
                                    headers=self.headers, json={"frame_ids": [second]})
        self.assertEqual(appended.status_code, 200)
        closed = self.client.post(f"/api/photo-analysis/jobs/{job_id}/close", headers=self.headers)
        self.assertEqual(closed.status_code, 200)
        job = self._wait_state(job_id)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(job["counts"]["done"], 2)
        rejected = self.client.post(f"/api/photo-analysis/jobs/{job_id}/frames",
                                    headers=self.headers, json={"frame_ids": [self._add_frame()]})
        self.assertEqual(rejected.status_code, 409)

    def test_queue_transient_failure_is_retried(self):
        self._fast_retries()
        flaky = self._add_frame(payload=b"FLAKY-image-data")
        job_id = self.manager.submit_frames(self.task["task_id"], [flaky])
        job = self._wait_state(job_id)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(job["counts"]["done"], 1)
        self.assertEqual(job["items"][0]["attempts"], 1)  # attempts 计失败次数：失败 1 次后重试成功
        self.assertEqual(len(job["results"]["images"]), 1)
        # stub 的 seen 日志证明该帧确实被识别了两次（首败+重试成功）。
        seen_path = (self.manager.tasks_root / self.task["task_id"] / "media" / "photo-analysis"
                     / job_id / "outputs" / "stub-seen.txt")
        self.assertEqual(seen_path.read_text(encoding="utf-8").count("\n"), 2)

    def test_queue_persistent_failure_is_isolated(self):
        self._fast_retries()
        bad = self._add_frame(payload=b"FAIL-image-data")
        good = self._add_frame()
        job_id = self.manager.submit_frames(self.task["task_id"], [bad, good])
        job = self._wait_state(job_id)
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["counts"], {"total": 2, "done": 1, "failed": 1, "pending": 0, "running": 0, "cancelled": 0})
        failed_item = next(item for item in job["items"] if item["state"] == "failed")
        self.assertEqual(failed_item["attempts"], photo_analysis_module.QUEUE_MAX_ATTEMPTS)
        self.assertIn("persistent failure", failed_item["error"])
        # 失败帧可一键重试。
        retried = self.client.post(f"/api/photo-analysis/jobs/{job_id}/retry-failed", headers=self.headers)
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["retried"], 1)
        retried_job = self._wait_state(job_id)
        self.assertEqual(retried_job["state"], "done")
        self.assertEqual(retried_job["counts"]["failed"], 1)  # stub 仍按内容失败，隔离依旧

    def test_queue_circuit_breaker_aborts(self):
        self._fast_retries()
        for _ in range(photo_analysis_module.QUEUE_CIRCUIT_FAILURES):
            self._add_frame(payload=b"FAIL-image-data")
        frames = [item["frame_id"] for item in self.store.frames(self.task["task_id"])]
        job_id = self.manager.submit_frames(self.task["task_id"], frames)
        job = self._wait_state(job_id, timeout=60)
        self.assertEqual(job["state"], "error")
        self.assertIn("系统性故障", job["error"])

    def test_queue_cancel_while_running(self):
        slow = self._add_frame(payload=b"SLOW-image-data")
        job_id = self.manager.submit_frames(self.task["task_id"], [slow])
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.manager.job(job_id)["counts"]["running"] >= 1:
                break
            time.sleep(0.05)
        else:
            self.fail("slow frame never started")
        cancelled = self.client.post(f"/api/photo-analysis/jobs/{job_id}/cancel", headers=self.headers)
        self.assertEqual(cancelled.status_code, 200)
        started = time.time()
        job = self._wait_state(job_id, timeout=10)
        self.assertLess(time.time() - started, 4)  # 取消不等 SLOW 帧跑完
        self.assertEqual(job["state"], "cancelled")

    def test_queue_batch_timeout_retries_then_fails(self):
        self._fast_retries()
        timeout_patch = patch.object(photo_analysis_module, "QUEUE_BATCH_TIMEOUT_BASE", {ONNX_MODEL_ID: 0.3})
        timeout_patch.start()
        self.addCleanup(timeout_patch.stop)
        per_image_patch = patch.object(photo_analysis_module, "QUEUE_BATCH_TIMEOUT_PER_IMAGE", {ONNX_MODEL_ID: 0.0})
        per_image_patch.start()
        self.addCleanup(per_image_patch.stop)
        slow = self._add_frame(payload=b"SLOW-image-data")
        job_id = self.manager.submit_frames(self.task["task_id"], [slow])
        job = self._wait_state(job_id, timeout=60)
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["counts"]["failed"], 1)
        self.assertIn("超时", job["items"][0]["error"])

    def test_queue_recovery_after_restart(self):
        done_frame = self._add_frame()
        pending_frame = self._add_frame()
        job_dir = self.manager.tasks_root / self.task["task_id"] / "media" / "photo-analysis"
        # 手工摆放一个"后端重启时中断"的任务：一帧已完成、一帧未跑。
        fabricated = job_dir / "20260101000000-deadbeef"
        outputs = fabricated / "outputs"
        outputs.mkdir(parents=True)
        frames = self.store.frames(self.task["task_id"])
        done_item = frames[0]
        pending_item = frames[1]
        success_line = {"name": done_item["file_name"], "sha256": "0" * 64, "width": 1, "height": 1,
                        "model_input_size": 256, "suspected_pixels": 1, "suspected_fraction": 1.0,
                        "suspected_patch_count": 1, "patch_area_pixels": [1],
                        "mean_confidence_on_suspected": 0.9,
                        "overlay": f"{Path(done_item['file_name']).stem}__overlay.png"}
        (outputs / "results.jsonl").write_text(json.dumps(success_line) + "\n", encoding="utf-8")
        (fabricated / "job.json").write_text(json.dumps({
            "job_id": "20260101000000-deadbeef", "state": "running", "mode": "frames",
            "task_id": self.task["task_id"], "model_id": ONNX_MODEL_ID,
            "context": {}, "closed": True, "created_at": "2026-01-01T00:00:00+00:00",
            "finished_at": None, "error": None,
            "items": [
                {"frame_id": done_item["frame_id"], "name": done_item["file_name"],
                 "file_name": done_item["file_name"], "meta": done_item, "state": "pending",
                 "error": None, "attempts": 0},
                {"frame_id": pending_item["frame_id"], "name": pending_item["file_name"],
                 "file_name": pending_item["file_name"], "meta": pending_item, "state": "running",
                 "error": None, "attempts": 0},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        resumed = self.manager.recover_jobs()
        self.assertEqual(resumed, 1)
        job = self._wait_state("20260101000000-deadbeef")
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["counts"]["done"], 2)
        # 已完成帧的结果来自重放的 results.jsonl，不重复识别。
        self.assertEqual(job["items"][0]["attempts"], 0)

    def test_queue_keeps_all_frames_even_with_legacy_discard_flag(self):
        clean = self._add_frame(payload=b"CLEAN-data")
        problem = self._add_frame(payload=TINY_PNG)
        # 截图帧只增不删：旧的 discard_clean_frames 标志必须被忽略。
        job_id = self.manager.submit_frames(self.task["task_id"], [clean, problem],
                                            context={"discard_clean_frames": True})
        job = self._wait_state(job_id)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(job["counts"]["done"], 2)
        remaining = {item["frame_id"] for item in self.store.frames(self.task["task_id"])}
        self.assertIn(clean, remaining)      # 无疑似积水的帧也保留
        self.assertIn(problem, remaining)    # 有疑似积水的帧保留

    def test_queue_second_submit_rejected_while_busy(self):
        self._add_frame(payload=b"SLOW-image-data")
        frames = [item["frame_id"] for item in self.store.frames(self.task["task_id"])]
        job_id = self.manager.submit_frames(self.task["task_id"], frames)
        second = self._add_frame()
        response = self.client.post("/api/photo-analysis/jobs", headers=self.headers, json={
            "task_id": self.task["task_id"], "frame_ids": [second]})
        self.assertEqual(response.status_code, 409)
        self._wait_state(job_id, timeout=30)


# 复检 stub：原样转存收到的 manifest，供测试断言 mask 路径是否跟着帧一起下发。
REMOTE_STUB_RUNNER = r'''
import base64, json, sys
from pathlib import Path

arguments = dict(zip(sys.argv[1::2], sys.argv[2::2]))
output_dir = Path(arguments["--output-dir"])
output_dir.mkdir(parents=True, exist_ok=True)
entries = json.loads(Path(arguments["--manifest"]).read_text(encoding="utf-8"))
(output_dir / "manifest-copy.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8QAAwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
with (output_dir / "results.jsonl").open("a", encoding="utf-8") as jsonl:
    for entry in entries:
        raw = entry if isinstance(entry, str) else entry.get("path", "")
        path = Path(raw)
        stem = path.stem
        (output_dir / f"{stem}__overlay.png").write_bytes(png)
        (output_dir / f"{stem}__suspected.png").write_bytes(png)
        (output_dir / f"{stem}__confidence.png").write_bytes(png)
        record = {"name": path.name, "sha256": "0" * 64, "width": 1, "height": 1,
                  "model_input_size": 256, "suspected_pixels": 1, "suspected_fraction": 1.0,
                  "suspected_patch_count": 1, "mean_confidence_on_suspected": 0.9,
                  "overlay": f"{stem}__overlay.png", "suspected_mask": f"{stem}__suspected.png",
                  "confidence_map": f"{stem}__confidence.png"}
        jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        jsonl.flush()
payload = {"model": {"default_model": "stub-remote", "engine": "stub", "task": "ponding", "water_class": 1,
                     "input_size": 1536, "model_sha256": "0" * 64},
           "images": [], "elapsed_seconds": 0.01, "interpretation": "stub-remote"}
(output_dir / "results.json").write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps(payload))
'''


class RemoteRecheckManifestTests(unittest.TestCase):
    """复检 job：manifest 必须把第一遍 YOLO 的疑似掩膜路径带给 GLM runner。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.base = base
        self.manager = _make_stub_manager(base, REMOTE_STUB_RUNNER)
        self.manager.yolo_python_path = Path(sys.executable)
        self.manager.remote_runner_path = self.manager.runner_path
        self.manager.frame_resolver = self._resolve_frame
        # 屏蔽本机真实 .env，让 remote 模型判定只取决于 stub 配置。
        config_patch = patch.object(photo_analysis_module, "_configured_value",
                                    lambda name, default="": "stub-remote-config")
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.manager_patch = patch.object(main, "photo_analysis", self.manager)
        self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)
        self.store = TaskStore(storage_dir=base / "tasks")
        self.tasks_patch = patch.object(main, "tasks", self.store)
        self.tasks_patch.start()
        self.addCleanup(self.tasks_patch.stop)
        self.client = TestClient(main.app, base_url="http://127.0.0.1:5173", client=("127.0.0.1", 50001))
        login_admin(self.client)
        self.headers = {"Origin": "http://127.0.0.1:5173"}
        self.task = self.store.create({"anchors": []}, "2026-10-23T08:00:00+08:00")

    def _resolve_frame(self, task_id: str, frame_id: str) -> dict:
        frame = next((item for item in self.store.frames(task_id) if item.get("frame_id") == frame_id), None)
        if frame is None:
            raise KeyError(frame_id)
        return {"name": frame["file_name"],
                "path": self.store.frame_directory(task_id) / frame["file_name"],
                "meta": {}}

    def _add_frame(self) -> str:
        frame = self.store.add_frame(self.task["task_id"], "frame.jpg", TINY_PNG, {
            "time_seconds": 1.0, "latitude": 31.6, "longitude": 119.9,
            "altitude_m": 60.0, "gimbal_pitch_deg": -90.0,
        })
        return frame["frame_id"]

    def _wait_done(self, job_id, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.manager.job(job_id)
            if job["state"] in ("done", "error", "cancelled"):
                return job
            time.sleep(0.05)
        self.fail("job did not finish in time")

    def _stored_manifest(self, job_id):
        manifest_copy = next((self.base / "tasks").glob(
            f"*/media/photo-analysis/{job_id}/outputs/manifest-copy.json"))
        return json.loads(manifest_copy.read_text(encoding="utf-8"))

    def _seed_first_pass_outputs(self, frame_file_name):
        outputs = (self.base / "tasks" / self.task["task_id"] / "media" / "photo-analysis"
                   / "20260101000000-firstpass" / "outputs")
        outputs.mkdir(parents=True, exist_ok=True)
        mask_name = frame_file_name.rsplit(".", 1)[0] + "__suspected.png"
        (outputs / mask_name).write_bytes(TINY_PNG)
        (outputs / "results.json").write_text(json.dumps(
            {"images": [{"name": frame_file_name, "suspected_mask": mask_name}]}), encoding="utf-8")
        return outputs / mask_name

    def test_recheck_manifest_carries_yolo_mask(self):
        frame_id = self._add_frame()
        frame = self.store.frames(self.task["task_id"])[0]
        mask_path = self._seed_first_pass_outputs(frame["file_name"])
        submit = self.client.post("/api/photo-analysis/jobs", headers=self.headers, json={
            "task_id": self.task["task_id"], "frame_ids": [frame_id],
            "model_id": "remote-glm-puddle", "recheck_of_job_id": "20260101000000-firstpass"})
        self.assertEqual(submit.status_code, 202, submit.text)
        job = self._wait_done(submit.json()["job_id"])
        self.assertEqual(job["state"], "done", job.get("error"))
        manifest = self._stored_manifest(job["job_id"])
        self.assertEqual(len(manifest), 1)
        self.assertEqual(Path(manifest[0]["path"]).name, frame["file_name"])
        self.assertEqual(Path(manifest[0]["mask"]), mask_path)

    def test_remote_manifest_without_recheck_omits_mask(self):
        frame_id = self._add_frame()
        submit = self.client.post("/api/photo-analysis/jobs", headers=self.headers, json={
            "task_id": self.task["task_id"], "frame_ids": [frame_id],
            "model_id": "remote-glm-puddle"})
        self.assertEqual(submit.status_code, 202, submit.text)
        job = self._wait_done(submit.json()["job_id"])
        self.assertEqual(job["state"], "done", job.get("error"))
        manifest = self._stored_manifest(job["job_id"])
        self.assertEqual(manifest, [{"path": manifest[0]["path"]}])
        self.assertEqual(Path(manifest[0]["path"]).name,
                         self.store.frames(self.task["task_id"])[0]["file_name"])

    def test_recheck_with_missing_first_pass_falls_back_to_whole_frame(self):
        frame_id = self._add_frame()
        submit = self.client.post("/api/photo-analysis/jobs", headers=self.headers, json={
            "task_id": self.task["task_id"], "frame_ids": [frame_id],
            "model_id": "remote-glm-puddle", "recheck_of_job_id": "20990101000000-gone"})
        self.assertEqual(submit.status_code, 202, submit.text)
        job = self._wait_done(submit.json()["job_id"])
        self.assertEqual(job["state"], "done", job.get("error"))
        manifest = self._stored_manifest(job["job_id"])
        self.assertEqual(sorted(manifest[0].keys()), ["path"])


if __name__ == "__main__":
    unittest.main()
