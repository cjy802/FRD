"""Traceable regional task candidates from manually transcribed official events."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .config import CLASSES


def read_sites(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到巡查点清单：{path}")
    required = {"site_id", "region", "task", "notes"}
    sites: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"sites 清单缺少列：{', '.join(sorted(required - set(reader.fieldnames or [])))}")
        for line, row in enumerate(reader, start=2):
            site_id = (row["site_id"] or "").strip()
            region = (row["region"] or "").strip()
            task = (row["task"] or "").strip()
            if not site_id or not region or task not in CLASSES:
                raise ValueError(f"sites 第 {line} 行缺少地点、地区或使用了未知任务")
            if site_id in seen:
                raise ValueError(f"sites 的 site_id 重复：{site_id}")
            seen.add(site_id)
            site: dict[str, Any] = {"site_id": site_id, "region": region, "task": task, "notes": (row["notes"] or "").strip()}
            for coordinate in ("latitude", "longitude"):
                raw = (row.get(coordinate) or "").strip()
                if raw:
                    value = float(raw)
                    if coordinate == "latitude" and not -90 <= value <= 90 or coordinate == "longitude" and not -180 <= value <= 180:
                        raise ValueError(f"sites 第 {line} 行 {coordinate} 超出范围")
                    site[coordinate] = value
            sites.append(site)
    return sites


def regional_plan(event: dict[str, Any], sites: list[dict[str, Any]], task: str | None = None) -> dict[str, Any]:
    if task is not None and task not in CLASSES:
        raise ValueError(f"未知任务：{task}")
    # The event region is manually transcribed; never infer exact exposure from a city-level name.
    candidates = []
    for site in sites:
        if site["region"] != event["region"] or task is not None and site["task"] != task:
            continue
        candidates.append({
            **site,
            "reason": "预警／水情适用区域与人工登记的站点行政区完全一致；点位状态仍待影像与人员核查。",
        })
    return {
        "event_context": event,
        "region_match_method": "exact_match_on_manually_entered_region_names",
        "candidate_count": len(candidates),
        "candidate_sites": candidates,
        "interpretation": "这是区域级待巡查清单，不是点位受灾概率、任务自动起飞指令或飞行许可。",
    }
