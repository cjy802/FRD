"""Local, Windows-only bridge from a DJI Fly KMZ draft to an RC 2 MTP slot.

The browser selects a waypoint folder on the remote; paths, PowerShell
arguments and the MTP transfer are owned by this module, never by the request.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
SCRIPT_PATH = TOOLS_DIR / "rc2_mtp_upload.ps1"
MAX_FLY_WAYPOINTS = 200
MAX_REMEMBERED_JOBS = 100
JOB_RETENTION = timedelta(hours=24)
_SAFE_KMZ_NAME = re.compile(r"^[^/\\\x00-\x1f\x7f:<>|?*]{1,124}\.kmz$", re.IGNORECASE)
_UUID_FOLDER = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_KML_POINT = "{http://www.opengis.net/kml/2.2}Placemark"


class Rc2Error(Exception):
    def __init__(self, message: str, *, http_status: int = 503, result: dict[str, Any] | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.result = result or {}


def import_root() -> Path:
    override = os.getenv("DJI_RC2_IMPORT_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "LowAltitudeWorkbench" / "DJI" / "imports"


def _interactive_windows_session() -> bool:
    if platform.system() != "Windows":
        return False
    session_id = ctypes.c_uint32()
    try:
        if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
            return False
    except (AttributeError, OSError):
        return False
    return session_id.value != 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_target_name(name: str) -> bool:
    return bool(_SAFE_KMZ_NAME.fullmatch(name)) and ".." not in name and name == name.strip()


def validate_folder_name(name: str) -> bool:
    return bool(_UUID_FOLDER.fullmatch(name))


def count_fly_waypoints(payload: bytes) -> int:
    """Count actual exported waypoints after the KMZ builder has simplified them."""
    with ZipFile(BytesIO(payload)) as archive:
        waylines = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
    return len(waylines.findall(f".//{_KML_POINT}"))


def ensure_fly_waypoint_limit(payload: bytes) -> int:
    try:
        count = count_fly_waypoints(payload)
    except (OSError, ValueError, KeyError, BadZipFile, ElementTree.ParseError) as error:
        raise Rc2Error(f"无法检查 DJI Fly 航点数量：{error}", http_status=422) from error
    if count > MAX_FLY_WAYPOINTS:
        raise Rc2Error(
            f"DJI Fly 单条航点任务最多 {MAX_FLY_WAYPOINTS} 点；当前 KMZ 包含 {count} 点。请减少航线节点后重试。",
            http_status=422,
        )
    return count


def _last_json(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.lstrip("\ufeff").splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


class Rc2ImportManager:
    def __init__(self, *, script_path: Path = SCRIPT_PATH):
        self.script_path = script_path
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_id: str | None = None

    def _prune_jobs_locked(self) -> None:
        cutoff = datetime.now(timezone.utc) - JOB_RETENTION
        for job_id, job in list(self._jobs.items()):
            finished = job.get("finished_at")
            if job_id != self._active_id and finished and datetime.fromisoformat(finished) < cutoff:
                del self._jobs[job_id]
        completed = sorted(
            (job for job in self._jobs.values() if job.get("finished_at") and job["job_id"] != self._active_id),
            key=lambda job: job["finished_at"],
        )
        for job in completed[:max(0, len(self._jobs) - MAX_REMEMBERED_JOBS)]:
            del self._jobs[job["job_id"]]

    def status(self) -> dict[str, Any]:
        windows = platform.system() == "Windows"
        interactive = _interactive_windows_session() if windows else False
        script_available = self.script_path.is_file()
        powershell_available = shutil.which("powershell.exe") is not None if windows else False
        with self._lock:
            busy = self._active_id is not None
        if not windows:
            message = "RC 2 的 MTP 导入只支持 Windows 本机。"
        elif not interactive:
            message = "请在当前登录用户的交互式 Windows 会话中启动后端，不能作为系统服务运行。"
        elif not script_available:
            message = "找不到 tools/rc2_mtp_upload.ps1。"
        elif not powershell_available:
            message = "找不到 Windows PowerShell（powershell.exe）。"
        else:
            message = "本机导入组件已就绪；请连接并解锁 DJI RC 2。"
        return {
            "available": windows and interactive and script_available and powershell_available,
            "platform": "windows" if windows else platform.system().lower(),
            "message": message,
            "generation_directory": str(import_root()),
            "script_available": script_available,
            "busy": busy,
        }

    def _require_runtime(self, *, for_upload: bool) -> None:
        state = self.status()
        if state["platform"] != "windows" or not _interactive_windows_session():
            raise Rc2Error(state["message"])
        if not state["script_available"] or shutil.which("powershell.exe") is None:
            raise Rc2Error(state["message"])

    def _run_script(self, args: list[str], *, timeout: int) -> dict[str, Any]:
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Sta", "-File", str(self.script_path), *args, "-Json",
        ]
        try:
            completed = subprocess.run(
                command, shell=False, capture_output=True, text=True,
                encoding="utf-8-sig", errors="replace", timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as error:
            result = {"remote_state": "unknown"} if "-SourceKmz" in args else None
            raise Rc2Error("RC 2 MTP 操作超时；生成的 KMZ 已保留在本机。", http_status=504, result=result) from error
        except OSError as error:
            raise Rc2Error(f"无法启动 Windows PowerShell：{error}") from error
        result = _last_json(completed.stdout)
        if completed.returncode != 0 or not result or result.get("status") != "success":
            detail = (result or {}).get("message") or completed.stderr.strip() or "RC 2 脚本没有返回有效的 JSON 结果。"
            raise Rc2Error(detail, result=result)
        return result

    @staticmethod
    def _safe_subfolder(path: str) -> str | None:
        # 空串表示 kmz 就在文件夹根层，是合法位置；仅拒绝包含 . / .. 的路径。
        segments = [segment for segment in path.replace("\\", "/").split("/") if segment]
        if any(segment in {".", ".."} for segment in segments):
            return None
        return "/".join(segments)

    @staticmethod
    def _folder_entries(items: Any) -> list[dict[str, Any]]:
        """Normalize the script's waypoint/<UUID>/ folder listing."""
        entries: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return entries
        seen: set[tuple[str, str]] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            folder, kmz_name, subpath = item.get("folder"), item.get("name"), item.get("subpath")
            if not (isinstance(folder, str) and validate_folder_name(folder)):
                continue
            if not (isinstance(kmz_name, str) and validate_target_name(kmz_name)):
                continue
            subfolder = Rc2ImportManager._safe_subfolder(subpath) if isinstance(subpath, str) else None
            if subfolder is None:
                continue
            key = (folder.casefold(), kmz_name.casefold())
            if key in seen:
                continue
            seen.add(key)
            entries.append({
                "folder": folder,
                "kmz_name": kmz_name,
                "subpath": subfolder,
                "name_matches_folder": kmz_name.casefold() == f"{folder}.kmz".casefold(),
            })
        return entries

    def targets(self) -> dict[str, Any]:
        self._require_runtime(for_upload=False)
        with self._lock:
            if self._active_id is not None:
                raise Rc2Error("已有 RC 2 导入正在运行，请完成后再刷新占位航线。", http_status=409)
        result = self._run_script(["-ListOnly"], timeout=90)
        targets = result.get("targets")
        if not isinstance(targets, list):
            raise Rc2Error("RC 2 脚本没有返回占位航线列表。")
        safe_targets = [
            {key: item[key] for key in ("name", "size", "type") if key in item}
            for item in targets
            if isinstance(item, dict) and isinstance(item.get("name"), str)
            and validate_target_name(item["name"])
            and not (item.get("is_folder") or item.get("isFolder"))
        ]
        return {
            "device_name": result.get("device_name") or "DJI RC 2",
            "waypoint_path": result.get("waypoint_path") or "Android/data/dji.go.v5/files/waypoint",
            "targets": safe_targets,
            "folders": self._folder_entries(result.get("folders")),
        }

    def start(self, payload: bytes, target_file_name: str, target_folder: str | None = None) -> dict[str, Any]:
        if not validate_target_name(target_file_name):
            raise Rc2Error("请选择 RC 2 上已有的 .kmz 占位航线文件。", http_status=422)
        if target_folder is not None and not validate_folder_name(target_folder):
            raise Rc2Error("占位航线文件夹编号无效。", http_status=422)
        ensure_fly_waypoint_limit(payload)
        self._require_runtime(for_upload=True)
        with self._lock:
            if self._active_id is not None:
                raise Rc2Error("已有 RC 2 导入正在运行，请等待其完成。", http_status=409)
            # Reserve before MTP enumeration, so another request cannot race this one.
            self._active_id = "checking"
        job_id: str | None = None
        try:
            current_targets = self.targets_for_import()
            if target_folder is not None:
                matched_folders = [entry for entry in current_targets["folders"]
                                   if entry["folder"].casefold() == target_folder.casefold()]
                if len(matched_folders) != 1:
                    raise Rc2Error("所选占位航线文件夹已不在 RC 2 上；请刷新列表后重新选择。", http_status=409)
                entry = matched_folders[0]
                if not entry["name_matches_folder"]:
                    raise Rc2Error(
                        f"文件夹 {entry['folder']} 里的 KMZ 文件名是 {entry['kmz_name']}，"
                        "与文件夹不一致；实验表明这样替换不会被 DJI Fly 识别，请使用 DJI Fly 生成的占位航线文件夹。",
                        http_status=422,
                    )
                canonical_name = entry["kmz_name"]
                # 脚本的 -RemoteSubFolder 相对 waypoint 根；kmz 相对 UUID 文件夹，
                # 所以要拼上文件夹本身（subpath 为空表示 kmz 就在文件夹根层）。
                target_subpath = "/".join(part for part in (target_folder, entry["subpath"]) if part)
            else:
                matched = [item["name"] for item in current_targets["targets"] if item["name"].casefold() == target_file_name.casefold()]
                if len(matched) != 1:
                    raise Rc2Error("所选占位航线已不在 RC 2 上；请刷新列表后重新选择。", http_status=409)
                canonical_name = matched[0]
                target_subpath = None
            job_id = uuid4().hex
            job = {
                "job_id": job_id,
                "status": "queued",
                "stage": "queued",
                "message": "已确认 RC 2 占位航线，等待生成 KMZ。",
                "target_file_name": canonical_name,
                "target_folder": target_folder,
                "target_subpath": target_subpath,
                "source_path": None,
                "backup_directory": str(import_root().parent / "backups" / job_id),
                "backup_path": None,
                "source_sha256": None,
                "readback_sha256": None,
                "remote_state": None,
                "started_at": _now(),
                "finished_at": None,
            }
            with self._lock:
                self._prune_jobs_locked()
                self._jobs[job_id] = job
                self._active_id = job_id
            queued = dict(job)
            threading.Thread(target=self._run_job, args=(job_id, payload), daemon=True).start()
            return queued
        except Exception:
            with self._lock:
                if self._active_id == "checking":
                    self._active_id = None
                elif job_id is not None and self._active_id == job_id:
                    self._jobs.pop(job_id, None)
                    self._active_id = None
            raise

    def targets_for_import(self) -> dict[str, Any]:
        """Enumerate while the single-job reservation is held."""
        self._require_runtime(for_upload=False)
        result = self._run_script(["-ListOnly"], timeout=90)
        items = result.get("targets")
        if not isinstance(items, list):
            raise Rc2Error("RC 2 脚本没有返回占位航线列表。")
        return {"targets": [
            item for item in items
            if isinstance(item, dict) and isinstance(item.get("name"), str)
            and validate_target_name(item["name"])
            and not (item.get("is_folder") or item.get("isFolder"))
        ], "folders": self._folder_entries(result.get("folders"))}

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    def _run_job(self, job_id: str, payload: bytes) -> None:
        with self._lock:
            target_name = self._jobs[job_id]["target_file_name"]
            backup_directory = self._jobs[job_id]["backup_directory"]
            target_subpath = self._jobs[job_id].get("target_subpath")
        source_path: Path | None = None
        try:
            self._update(job_id, status="running", stage="generating", message="正在生成并保存 DJI Fly KMZ。")
            work_dir = import_root() / job_id
            work_dir.mkdir(parents=True, exist_ok=False)
            source_path = work_dir / target_name
            with source_path.open("xb") as destination:
                destination.write(payload)
            source_hash = hashlib.sha256(payload).hexdigest()
            self._update(job_id, source_path=str(source_path), source_sha256=source_hash,
                         stage="uploading",
                         message="KMZ 已保存，正在备份并替换 RC 2 占位航线；Windows 弹出“替换文件”确认框属正常步骤，请点击“替换/是”。")
            script_args = ["-SourceKmz", str(source_path), "-RemoteFileName", target_name, "-ReplaceExisting",
                           "-BackupRoot", backup_directory]
            if target_subpath:
                script_args += ["-RemoteSubFolder", target_subpath]
            result = self._run_script(script_args, timeout=600)
            if (result.get("remote_state") != "verified"
                    or result.get("source_sha256", "").lower() != source_hash
                    or result.get("readback_sha256", "").lower() != source_hash):
                raise Rc2Error("RC 2 回读 SHA-256 未通过；请核对备份和设备文件。", result=result)
            self._update(
                job_id, status="succeeded", stage="verified",
                message="RC 2 文件已替换并通过回读 SHA-256 校验；请在 DJI Fly 中核对航线。",
                backup_path=result.get("backup_path"),
                readback_sha256=result.get("readback_sha256"),
                remote_state="verified", finished_at=_now(),
            )
        except Exception as error:
            result = error.result if isinstance(error, Rc2Error) else {}
            message = str(error)
            if result.get("remote_state") in {"unknown", "uncertain"}:
                message += "；设备文件状态未确认，请依据备份文件人工核对 RC 2。"
            self._update(
                job_id, status="failed", stage="failed", message=message,
                source_path=str(source_path) if source_path else None,
                backup_path=result.get("backup_path"),
                readback_sha256=result.get("readback_sha256"),
                remote_state=result.get("remote_state"),
                restore_attempted=result.get("restore_attempted"),
                restore_verified=result.get("restore_verified"),
                failure_stage=result.get("failure_stage"),
                recovery_stage=result.get("recovery_stage"),
                finished_at=_now(),
            )
        finally:
            with self._lock:
                if self._active_id == job_id:
                    self._active_id = None

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._prune_jobs_locked()
            if job_id not in self._jobs:
                raise Rc2Error("RC 2 导入任务不存在或服务已重启。", http_status=404)
            return dict(self._jobs[job_id])


rc2_imports = Rc2ImportManager()
