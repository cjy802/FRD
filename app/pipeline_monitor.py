"""Self-healing monitor for the regional-corrector data pipelines.

Checks the three detached pipelines (backfill A, backfill B, ERA5 truth
download): log freshness plus live python processes decide health. A pipeline
whose process is gone gets relaunched (resume is built into every script); a
process that is alive but has produced no log output for two hours is
reported as hung and left alone. When all pipelines finish cleanly the
monitor runs the pair join and fits the regional corrector into the staging
directory ``Web/mode/output_region`` — production only activates once the
artifact is copied to ``output_production`` by hand.

Exit prints one status block per pipeline plus the completion phase; designed
to be invoked every 30 minutes by a scheduler.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
PROJECT = APP.parent
DATA = PROJECT / "FuXi/DATA"
ERA5_DIR = DATA / "era5_region"
STAGING = PROJECT / "Web/mode/output_region"
PY = r"D:\anaconda\envs\fuxi_onnx\python.exe"
STALE_MIN = 45          # no log write for this long -> suspicious
HUNG_MIN = 120          # alive but silent this long -> alert only
RERUN_CAP = 3           # max automatic reruns per pipeline after failures
DETACHED = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0))

PIPELINES = {
    "backfill_a": {
        "script": "backfill_gfs_fuxi.py",
        "args": ["--start", "2024-01-01", "--end", "2025-05-31", "--fetch-workers", "10",
                 "--output", str(DATA / "fuxi2_gfs_region_forecast_a.csv"), "--execute"],
        "log": DATA / "backfill_a.log", "err": DATA / "backfill_a.err.log",
        "pattern": "fuxi2_gfs_region_forecast_a.csv",
    },
    "backfill_b": {
        "script": "backfill_gfs_fuxi.py",
        "args": ["--start", "2025-06-01", "--end", "2026-09-05", "--fetch-workers", "10",
                 "--output", str(DATA / "fuxi2_gfs_region_forecast_b.csv"), "--execute"],
        "log": DATA / "backfill_b.log", "err": DATA / "backfill_b.err.log",
        "pattern": "fuxi2_gfs_region_forecast_b.csv",
    },
    "era5_truth": {
        "script": "download_era5_region.py",
        "args": ["--start", "2024-01", "--end", "2026-09", "--execute"],
        "log": DATA / "era5_download.log", "err": DATA / "era5_download.err.log",
        "pattern": "download_era5_region.py",
    },
}


def process_count(pattern: str) -> int:
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


def launch(name: str) -> None:
    spec = PIPELINES[name]
    with open(spec["log"], "wb") as out, open(spec["err"], "wb") as err:
        subprocess.Popen([PY, "-B", str(APP / spec["script"]), *spec["args"]],
                         cwd=str(APP), stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                         creationflags=DETACHED)


def rerun_count(name: str) -> int:
    marker = DATA / f"monitor_reruns_{name}.txt"
    try:
        return int(marker.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def bump_rerun(name: str) -> None:
    marker = DATA / f"monitor_reruns_{name}.txt"
    marker.write_text(str(rerun_count(name) + 1), encoding="utf-8")


def reset_rerun(name: str) -> None:
    marker = DATA / f"monitor_reruns_{name}.txt"
    marker.unlink(missing_ok=True)


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
            return "HUNG", f"进程存活但日志 {age:.0f} 分钟无更新，需人工查看"
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
    fit = subprocess.run([PY, "-B", str(PROJECT / "Web/mode/fit_region_corrector.py"),
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
