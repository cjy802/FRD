"""Self-healing monitor for the regional-corrector data pipelines.

Runs on Windows (local) or Linux (cloud GPU box, /root/autodl-tmp/backfill).
Checks the three pipelines (backfill A, backfill B, ERA5 truth download) by
log freshness plus live python processes: a pipeline whose process is gone
gets relaunched (resume is built into every script); a process that is alive
but silent for two hours is reported as hung and left alone. When all
pipelines finish cleanly the monitor runs the pair join and fits the regional
corrector into the staging directory ``Web/mode/output_region`` — production
only activates once the artifact is copied to ``output_production`` by hand.

Prints one status block per pipeline plus the completion phase; designed to
be invoked every 30 minutes by a scheduler.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

POSIX = os.name == "posix"
if POSIX:
    APP = Path("/root/autodl-tmp/backfill/app")
    PY = "/root/miniconda3/bin/python"
    STAGING = APP.parent / "Web/mode/output_region"
else:
    APP = Path(__file__).resolve().parent
    PY = r"D:\anaconda\envs\fuxi_onnx\python.exe"
    STAGING = APP.parents[1] / "Web/mode/output_region"
PROJECT = APP.parent
DATA = PROJECT / "FuXi/DATA"
ERA5_DIR = DATA / "era5_region"
STALE_MIN = 45          # no log write for this long -> suspicious
HUNG_MIN = 120          # alive but silent this long -> alert only
RERUN_CAP = 3           # max automatic reruns per pipeline after failures

PIPELINES = {
    "backfill_a": {
        "script": "backfill_gfs_fuxi.py",
        "args": ["--start", "2024-01-17", "--end", "2025-05-31", "--fetch-workers", "12",
                 "--output", str(DATA / "fuxi2_gfs_region_forecast_a.csv"), "--execute"],
        "log": DATA / "backfill_a.log", "err": DATA / "backfill_a.err.log",
        "pattern": "fuxi2_gfs_region_forecast_a.csv",
    },
    "backfill_b": {
        "script": "backfill_gfs_fuxi.py",
        "args": ["--start", "2025-06-01", "--end", "2026-09-05", "--fetch-workers", "12",
                 "--output", str(DATA / "fuxi2_gfs_region_forecast_b.csv"), "--execute"],
        "log": DATA / "backfill_b.log", "err": DATA / "backfill_b.err.log",
        "pattern": "fuxi2_gfs_region_forecast_b.csv",
    },
    "era5_truth": {
        "script": "download_era5_region.py",
        "args": ["--start", "2024-01", "--end", "2026-09", "--execute"],
        "log": DATA / "era5_download.log", "err": DATA / "era5_download.err.log",
        "pattern": "python -B download_era5_region.py",
    },
}


def process_count(pattern: str) -> int:
    if POSIX:
        try:
            completed = subprocess.run(["pgrep", "-fc", pattern], capture_output=True, text=True, timeout=30)
            return int(completed.stdout.strip() or 0)
        except (ValueError, subprocess.SubprocessError):
            return -1
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{pattern}*' }} | Measure-Object | "
        "Select-Object -ExpandProperty Count"
    )
    try:
        completed = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                   capture_output=True, text=True, timeout=90)
        return int(completed.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return -1


def log_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def age_minutes(path: Path) -> float:
    try:
        return (time.time() - path.stat().st_mtime) / 60
    except OSError:
        return float("inf")


def posix_nvidia_libs() -> str:
    """LD_LIBRARY_PATH covering pip nvidia wheels and conda's own lib dir."""
    import glob

    candidates = sorted(glob.glob(os.path.join(os.path.dirname(PY), "..", "lib", "python*", "site-packages", "nvidia", "*", "lib")))
    conda_lib = os.path.join(os.path.dirname(PY), "..", "lib")
    paths = [os.path.normpath(path) for path in candidates]
    if os.path.isdir(conda_lib):
        paths.append(os.path.normpath(conda_lib))
    return os.pathsep.join(paths)


def launch(name: str) -> None:
    spec = PIPELINES[name]
    out = open(spec["log"], "wb")
    err = open(spec["err"], "wb")
    env = None
    kwargs = {}
    if POSIX:
        kwargs["start_new_session"] = True
        libs = posix_nvidia_libs()
        if libs:
            env = os.environ.copy()
            env["LD_LIBRARY_PATH"] = libs + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    else:
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.Popen([PY, "-B", str(APP / spec["script"]), *spec["args"]],
                     cwd=str(APP), stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env, **kwargs)


def rerun_count(name: str) -> int:
    marker = DATA / f"monitor_reruns_{name}.txt"
    try:
        return int(marker.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def bump_rerun(name: str) -> None:
    (DATA / f"monitor_reruns_{name}.txt").write_text(str(rerun_count(name) + 1), encoding="utf-8")


def reset_rerun(name: str) -> None:
    (DATA / f"monitor_reruns_{name}.txt").unlink(missing_ok=True)


def parse_done(text: str) -> tuple[bool, int]:
    """Return (finished, failed_count) from a pipeline's final summary line."""
    for line in text.splitlines():
        if line.startswith("Done:"):
            try:
                failed = int(line.rsplit("failed", 1)[0].rstrip(" .,").rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                failed = 0
            return True, failed
    return False, 0


def kill_pipeline(pattern: str) -> None:
    if POSIX:
        subprocess.run(["pkill", "-f", pattern], capture_output=True, timeout=30)
    else:
        command = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{pattern}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, timeout=90)


def check(name: str) -> tuple[str, str]:
    spec = PIPELINES[name]
    text = log_text(spec["log"])
    age = age_minutes(spec["log"])
    finished, failed = parse_done(text)

    if name == "era5_truth":
        months = len(list(ERA5_DIR.glob("era5_surface_box_*.csv")))
        if months >= 33:
            reset_rerun(name)
            return "COMPLETE", f"{months}/33 月真值已落盘"
        if finished and age > STALE_MIN and rerun_count(name) < RERUN_CAP:
            bump_rerun(name)
            launch(name)
            return "RERUN", f"退出但有缺口({months}/33)，已重启重试"
        if finished:
            return "INCOMPLETE", f"{months}/33 月，且重试次数已达上限，需人工介入"
    else:
        if finished and failed == 0:
            reset_rerun(name)
            return "COMPLETE", "全部日期完成、0 失败"
        if finished and failed > 0 and age > STALE_MIN and rerun_count(name) < RERUN_CAP:
            bump_rerun(name)
            launch(name)
            return "RERUN", f"退出时 {failed} 个失败日期，已重启重试"
        if finished:
            return "INCOMPLETE", f"{failed} 个失败日期，重试上限已到，需人工介入"

    alive = process_count(spec["pattern"])
    if alive > 0:
        if age > HUNG_MIN:
            # A silently wedged process (no log, no sockets) never recovers by
            # itself; kill and relaunch rather than only alerting. ERA5 is
            # exempt: cdsapi polls an accepted queue job silently for hours,
            # and a restart would waste that queued slot.
            if name == "era5_truth":
                return "HUNG", f"进程存活但日志 {age:.0f} 分钟无更新（CDS 队列轮询中，不自动重启）"
            if rerun_count(name) < RERUN_CAP:
                bump_rerun(name)
                kill_pipeline(spec["pattern"])
                time.sleep(5)
                launch(name)
                return "HUNG-RESTARTED", f"日志 {age:.0f} 分钟无更新，已杀掉重启（断点续传）"
            return "HUNG", f"进程存活但日志 {age:.0f} 分钟无更新，且重试上限已到，需人工介入"
        progress = ""
        appended = sum(1 for line in text.splitlines() if "rows appended" in line)
        if appended:
            progress = f"，本次运行已完成 {appended} 个日期"
        return "RUNNING", f"进程存活，日志 {age:.0f} 分钟前更新{progress}"
    if age > STALE_MIN:
        if rerun_count(name) < RERUN_CAP:
            bump_rerun(name)
            launch(name)
            return "RESTARTED", f"进程消失且日志停滞 {age:.0f} 分钟，已重新拉起"
        return "DEAD", "进程消失，重试上限已到，需人工介入"
    return "STARTING", f"日志 {age:.0f} 分钟前更新，进程计数 {alive}"


def completion_phase(statuses: dict[str, str]) -> str:
    flag = DATA / "monitor_finished.flag"
    if not all(state == "COMPLETE" for state in statuses.values()):
        return "数据仍在积累，跳过训练阶段"
    if flag.is_file():
        return "配对与训练已完成（见 Web/mode/output_region），等待人工验收后复制到 output_production 上线"
    print("[monitor] 三线全部完成，开始配对 + 暂存训练...")
    pair = subprocess.run([PY, "-B", str(APP / "pair_gfs_region.py")],
                          cwd=str(APP), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=1800)
    print(pair.stdout[-1500:])
    if pair.returncode:
        return "配对失败：" + (pair.stderr or "")[-500:]
    fit = subprocess.run([PY, "-B", str(APP.parent / "Web/mode/fit_region_corrector.py"),
                          "--test-year", "2026", "--output-dir", str(STAGING)],
                         cwd=str(APP), capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=3600)
    print(fit.stdout[-2500:])
    if fit.returncode:
        return "训练失败：" + (fit.stderr or "")[-500:]
    flag.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    return ("配对+训练完成，产物在 Web/mode/output_region/corrector_region_ridge.npz；"
            "确认指标后复制到 Web/mode/output_production/ 即自动上线（无需重启后端）")


def main() -> int:
    stamp = time.strftime("%m-%d %H:%M")
    statuses: dict[str, str] = {}
    for name in PIPELINES:
        state, detail = check(name)
        statuses[name] = state
        print(f"[{stamp}] {name}: {state} — {detail}", flush=True)
    print(f"[{stamp}] completion: {completion_phase(statuses)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
