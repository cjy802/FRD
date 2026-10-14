"""CSV scene manifest. Paths are relative to the manifest, not the shell cwd."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .config import CLASSES
from .images import parse_timestamp


@dataclass(frozen=True)
class Case:
    case_id: str
    site_id: str
    task: str
    split: str
    captured_at: str
    rgb: Path
    thermal: Path | None
    thermal_registration: str | Path | None
    mask: Path


def _resolve(base: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else base / candidate


def read_manifest(path: Path) -> list[Case]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到清单：{path}")
    required = {"case_id", "site_id", "task", "split", "captured_at", "rgb", "thermal", "thermal_registration", "mask"}
    base = path.parent
    cases: list[Case] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"manifest 缺少列：{', '.join(sorted(required - set(reader.fieldnames or [])))}")
        for line, row in enumerate(reader, start=2):
            case_id = (row["case_id"] or "").strip()
            site_id = (row["site_id"] or "").strip()
            task = (row["task"] or "").strip()
            split = (row["split"] or "").strip()
            rgb = (row["rgb"] or "").strip()
            mask = (row["mask"] or "").strip()
            if not case_id or not site_id or not rgb or not mask:
                raise ValueError(f"manifest 第 {line} 行缺少案例、地点、RGB 或标注路径")
            if case_id in seen:
                raise ValueError(f"manifest 的 case_id 重复：{case_id}")
            seen.add(case_id)
            if task not in CLASSES or split not in {"train", "val", "test"}:
                raise ValueError(f"manifest 第 {line} 行 task 或 split 无效")
            captured_at = parse_timestamp((row["captured_at"] or "").strip(), f"manifest 第 {line} 行 captured_at")
            thermal_value = (row["thermal"] or "").strip()
            registration_value = (row["thermal_registration"] or "").strip()
            if not thermal_value and registration_value:
                raise ValueError(f"manifest 第 {line} 行没有热图却指定了配准")
            registration: str | Path | None = None
            if registration_value:
                registration = "aligned" if registration_value == "aligned" else _resolve(base, registration_value)
            cases.append(Case(
                case_id=case_id,
                site_id=site_id,
                task=task,
                split=split,
                captured_at=captured_at,
                rgb=_resolve(base, rgb),
                thermal=_resolve(base, thermal_value) if thermal_value else None,
                thermal_registration=registration,
                mask=_resolve(base, mask),
            ))
    if not cases:
        raise ValueError("manifest 没有案例")
    return cases
