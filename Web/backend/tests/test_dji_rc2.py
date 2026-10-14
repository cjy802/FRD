import hashlib
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import dji_rc2
import main
from dji_kmz import build_dji_kmz


class Rc2BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "rc2_mtp_upload.ps1"
        self.helper = self.root / "silent_mtp_replace.exe"
        self.script.write_text("# test", encoding="utf-8")
        self.helper.write_bytes(b"test")
        self.manager = dji_rc2.Rc2ImportManager(script_path=self.script, helper_path=self.helper)
        self.runtime = [
            patch.object(dji_rc2.platform, "system", return_value="Windows"),
            patch.object(dji_rc2, "_interactive_windows_session", return_value=True),
            patch.object(dji_rc2.shutil, "which", return_value="powershell.exe"),
            patch.dict(dji_rc2.os.environ, {"DJI_RC2_IMPORT_ROOT": str(self.root / "imports")}),
        ]
        for item in self.runtime:
            item.start()
            self.addCleanup(item.stop)

    def _wait(self, job_id):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            job = self.manager.job(job_id)
            if job["status"] in {"succeeded", "failed"}:
                return job
            time.sleep(0.01)
        self.fail("RC 2 import worker did not finish")

    def test_script_contract_and_target_filter(self):
        result = {"status": "success", "device_name": "DJI RC 2", "waypoint_path": "waypoint", "targets": [
            {"name": "11111111-2222-3333-4444-555555555555.kmz", "size": 100},
            {"name": "../bad.kmz"}, {"name": "note.txt"}, {"name": "folder.kmz", "isFolder": True},
        ]}
        with patch.object(dji_rc2.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(result), stderr="",
        )) as run:
            found = self.manager.targets()
        self.assertEqual([item["name"] for item in found["targets"]], [
            "11111111-2222-3333-4444-555555555555.kmz",
        ])
        args, kwargs = run.call_args
        self.assertEqual(args[0][:7], ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Sta", "-File"])
        self.assertEqual(args[0][-2:], ["-ListOnly", "-Json"])
        self.assertIs(kwargs["shell"], False)

    def test_import_writes_per_job_kmz_and_checks_readback_hash(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.96, 31.68]], 100, 45, "fly")
        digest = hashlib.sha256(payload).hexdigest()
        name = "11111111-2222-3333-4444-555555555555.kmz"

        def run(command, **_kwargs):
            if "-ListOnly" in command:
                data = {"status": "success", "targets": [{"name": name}]}
            else:
                self.assertIn("-ReplaceExisting", command)
                self.assertEqual(command[command.index("-RemoteFileName") + 1], name)
                self.assertIn("backups", command[command.index("-BackupRoot") + 1])
                source = Path(command[command.index("-SourceKmz") + 1])
                self.assertEqual(source.read_bytes(), payload)
                data = {"status": "success", "remote_state": "verified", "readback_sha256": digest,
                        "source_sha256": digest, "backup_path": str(self.root / "backup" / name)}
            return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

        with patch.object(dji_rc2.subprocess, "run", side_effect=run):
            queued = self.manager.start(payload, name)
            job = self._wait(queued["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["source_sha256"], job["readback_sha256"])
        self.assertEqual(Path(job["source_path"]), self.root / "imports" / job["job_id"] / name)
        self.assertEqual(Path(job["backup_directory"]), self.root / "backups" / job["job_id"])
        self.assertTrue(Path(job["source_path"]).is_file())
        self.assertEqual(job["backup_path"], str(self.root / "backup" / name))

    def test_only_existing_listed_target_can_be_selected(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.96, 31.68]], 100, 45, "fly")
        with patch.object(self.manager, "targets_for_import", return_value={"targets": [{"name": "listed.kmz"}]}):
            with self.assertRaises(dji_rc2.Rc2Error) as context:
                self.manager.start(payload, "unlisted.kmz")
        self.assertEqual(context.exception.http_status, 409)
        self.assertFalse((self.root / "imports").exists())
        self.assertFalse(self.manager.status()["busy"])
        with self.assertRaises(dji_rc2.Rc2Error):
            self.manager.start(payload, "../listed.kmz")

    def test_failed_upload_keeps_kmz_and_backup_location(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.96, 31.68]], 100, 45, "fly")
        with patch.object(self.manager, "targets_for_import", return_value={"targets": [{"name": "listed.kmz"}]}), patch.object(
            self.manager, "_run_script", side_effect=dji_rc2.Rc2Error("MTP 写入失败", result={
                "backup_path": "C:/backup/listed.kmz", "remote_state": "restored",
            }),
        ):
            queued = self.manager.start(payload, "listed.kmz")
            job = self._wait(queued["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["backup_path"], "C:/backup/listed.kmz")
        self.assertEqual(job["remote_state"], "restored")
        self.assertEqual(Path(job["source_path"]).read_bytes(), payload)

    def test_uncertain_remote_state_is_explicit_and_job_history_is_bounded(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.96, 31.68]], 100, 45, "fly")
        with patch.object(self.manager, "targets_for_import", return_value={"targets": [{"name": "listed.kmz"}]}), patch.object(
            self.manager, "_run_script", side_effect=dji_rc2.Rc2Error("恢复失败", result={
                "backup_path": "C:/backup/listed.kmz", "remote_state": "unknown", "restore_attempted": True,
            }),
        ):
            queued = self.manager.start(payload, "listed.kmz")
            job = self._wait(queued["job_id"])
        self.assertIn("设备文件状态未确认", job["message"])
        self.assertTrue(job["restore_attempted"])
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with self.manager._lock:
            self.manager._jobs["expired"] = {"job_id": "expired", "finished_at": old}
        with self.assertRaises(dji_rc2.Rc2Error) as context:
            self.manager.job("expired")
        self.assertEqual(context.exception.http_status, 404)

    def test_script_error_json_preserves_backup_details(self):
        failure = {"status": "error", "stage": "restoring", "message": "恢复失败",
                   "backup_path": "C:/backup/listed.kmz", "remote_state": "unknown"}
        with patch.object(dji_rc2.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout=json.dumps(failure, ensure_ascii=False), stderr="",
        )):
            with self.assertRaises(dji_rc2.Rc2Error) as context:
                self.manager._run_script(["-ListOnly"], timeout=90)
        self.assertEqual(context.exception.result["backup_path"], "C:/backup/listed.kmz")
        self.assertEqual(str(context.exception), "恢复失败")

    def test_only_one_import_runs_at_a_time(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.96, 31.68]], 100, 45, "fly")
        held = threading.Event()
        entered = threading.Event()

        def wait_for_upload(_args, **_kwargs):
            entered.set()
            held.wait(timeout=3)
            raise dji_rc2.Rc2Error("模拟失败")

        with patch.object(self.manager, "targets_for_import", return_value={"targets": [{"name": "listed.kmz"}]}), patch.object(
            self.manager, "_run_script", side_effect=wait_for_upload,
        ):
            first = self.manager.start(payload, "listed.kmz")
            self.assertTrue(entered.wait(timeout=3))
            self.assertTrue(self.manager.status()["busy"])
            with self.assertRaises(dji_rc2.Rc2Error) as context:
                self.manager.start(payload, "listed.kmz")
            self.assertEqual(context.exception.http_status, 409)
            held.set()
            self.assertEqual(self._wait(first["job_id"])["status"], "failed")
        self.assertFalse(self.manager.status()["busy"])

    def test_worker_start_failure_releases_import_reservation(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.96, 31.68]], 100, 45, "fly")
        with patch.object(self.manager, "targets_for_import", return_value={"targets": [{"name": "listed.kmz"}]}), patch.object(
            dji_rc2.threading.Thread, "start", side_effect=RuntimeError("worker unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.manager.start(payload, "listed.kmz")
        self.assertFalse(self.manager.status()["busy"])
        self.assertEqual(self.manager._jobs, {})

    def test_missing_helper_is_reported_without_touching_device(self):
        self.helper.unlink()
        state = self.manager.status()
        self.assertFalse(state["available"])
        self.assertIn("build_silent_mtp_helper.ps1", state["message"])
        payload = build_dji_kmz([[119.95, 31.67], [119.96, 31.68]], 100, 45, "fly")
        with patch.object(dji_rc2.subprocess, "run") as run:
            with self.assertRaises(dji_rc2.Rc2Error):
                self.manager.start(payload, "listed.kmz")
            run.assert_not_called()

    def test_actual_fly_waypoint_limit(self):
        coords = [[119.95 + i * 0.0001, 31.67] for i in range(201)]
        settings = [{"export_anchor": True} for _ in coords]
        payload = build_dji_kmz(coords, 100, 45, "fly", settings)
        self.assertEqual(dji_rc2.count_fly_waypoints(payload), 201)
        with self.assertRaises(dji_rc2.Rc2Error) as context:
            dji_rc2.ensure_fly_waypoint_limit(payload)
        self.assertEqual(context.exception.http_status, 422)
        self.assertIn("200", str(context.exception))

    def test_local_http_guard_and_import_api(self):
        client = TestClient(main.app, base_url="http://127.0.0.1:5173", client=("127.0.0.1", 50000))
        self.assertEqual(client.get("/api/dji/rc2/status", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(client.get("/api/dji/rc2/status", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(client.get("/api/dji/rc2/status", headers={"Referer": "http://evil.example/"}).status_code, 403)
        self.assertEqual(client.post("/api/dji/rc2/import", json={}).status_code, 422)
        body = {"coordinates": [[119.95, 31.67], [119.96, 31.68]], "altitude_m": 100,
                "speed_kmh": 45, "drone_model": "fly", "target_file_name": "listed.kmz"}
        self.assertEqual(client.post("/api/dji/rc2/import", json=body,
                                     headers={"Origin": "http://evil.example"}).status_code, 403)
        def verified_upload(command, **_kwargs):
            source = Path(command[command.index("-SourceKmz") + 1])
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            return {"status": "success", "remote_state": "verified",
                    "source_sha256": digest, "readback_sha256": digest}

        with patch.object(main, "rc2_imports", self.manager), patch.object(
            self.manager, "targets_for_import", return_value={"targets": [{"name": "listed.kmz"}]}
        ), patch.object(self.manager, "_run_script", side_effect=verified_upload):
            response = client.post("/api/dji/rc2/import", json=body,
                                   headers={"Origin": "http://127.0.0.1:5173"})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertIn("job_id", response.json())
            self.assertEqual(self._wait(response.json()["job_id"])["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
