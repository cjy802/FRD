"""Persist locally saved route templates without recalculating their paths."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mission_routes import MAX_MISSION_ANCHORS, MAX_MISSION_VERTICES


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "user_data" / "route_templates.sqlite"
MAX_TEMPLATE_BYTES = 8 * 1024 * 1024


class TemplateNotFound(Exception):
    pass


class TemplateConflict(Exception):
    pass


class TemplateTooLarge(Exception):
    pass


class Anchor(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)

    @field_validator("id")
    @classmethod
    def nonempty_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("主点 ID 不能为空")
        return value


class PlanInput(BaseModel):
    anchors: list[Anchor] = Field(min_length=2, max_length=MAX_MISSION_ANCHORS)
    road_source: Literal["local", "online"]
    altitude_m: float = Field(ge=0, le=1000, allow_inf_nan=False)
    speed_kmh: float = Field(gt=0, le=200, allow_inf_nan=False)
    clearance_m: float = Field(ge=0, le=20, allow_inf_nan=False)

    @model_validator(mode="after")
    def unique_anchors(self):
        ids = [anchor.id for anchor in self.anchors]
        if len(ids) != len(set(ids)):
            raise ValueError("主点 ID 必须唯一")
        for first, second in zip(self.anchors, self.anchors[1:]):
            if first.latitude == second.latitude and first.longitude == second.longitude:
                raise ValueError("相邻主点不能重合")
        return self


class Geometry(BaseModel):
    type: Literal["LineString"]
    coordinates: list[tuple[
        float, float
    ]] = Field(min_length=2, max_length=MAX_MISSION_VERTICES)

    @field_validator("coordinates")
    @classmethod
    def valid_coordinates(cls, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        for longitude, latitude in points:
            if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                raise ValueError("路线坐标超出经纬度范围")
            if not (abs(longitude) < float("inf") and abs(latitude) < float("inf")):
                raise ValueError("路线坐标必须是有限数值")
        return points


class RouteSnapshot(BaseModel):
    # Keep planner metadata and future read-only display fields in the snapshot.
    model_config = ConfigDict(extra="allow")

    geometry: Geometry
    distance_m: float = Field(ge=0, allow_inf_nan=False)
    distance_km: float = Field(ge=0, allow_inf_nan=False)
    straight_distance_km: float = Field(ge=0, allow_inf_nan=False)
    estimated_minutes: float = Field(ge=0, allow_inf_nan=False)
    altitude_m: float = Field(ge=0, le=1000, allow_inf_nan=False)
    road_names: list[str]
    start_snap_m: float = Field(ge=0, allow_inf_nan=False)
    end_snap_m: float = Field(ge=0, allow_inf_nan=False)
    building_clearance_m: float | None = Field(ge=0, default=None, allow_inf_nan=False)
    data_source: str
    risk: dict[str, Any]
    anchors: list[Anchor] | None = None
    anchor_indices: list[int] = Field(min_length=2, max_length=MAX_MISSION_ANCHORS)
    segments: list[dict[str, Any]] | None = None


class WaypointSetting(BaseModel):
    altitude_m: float = Field(ge=0, le=1500, allow_inf_nan=False)
    heading_mode: Literal["followWayline", "manually", "fixed", "smoothTransition", "towardPOI"]
    heading_angle: float = Field(allow_inf_nan=False)
    gimbal_pitch_angle: float = Field(ge=-90, le=45, allow_inf_nan=False)
    export_anchor: bool = False
    poi: tuple[float, float, float] | None = None


class TemplatePayload(BaseModel):
    schema_version: Literal[1]
    plan_input: PlanInput
    route_snapshot: RouteSnapshot
    waypoint_settings: list[WaypointSetting] = Field(min_length=2, max_length=MAX_MISSION_VERTICES)
    dji_model: Literal["fly", "m30", "m3e"]

    @model_validator(mode="after")
    def matching_route(self):
        anchors = self.plan_input.anchors
        route = self.route_snapshot
        coordinates = route.geometry.coordinates
        indices = route.anchor_indices
        if len(indices) != len(anchors) or indices[0] != 0 or indices[-1] != len(coordinates) - 1:
            raise ValueError("路线主点索引与主点或路线坐标不匹配")
        if any(a >= b for a, b in zip(indices, indices[1:])):
            raise ValueError("路线主点索引必须严格递增")
        if len(self.waypoint_settings) != len(coordinates):
            raise ValueError("逐点设置数量必须与路线坐标数量一致")
        if route.anchors is not None and route.anchors != anchors:
            raise ValueError("路线快照主点与规划输入不一致")
        # A user may move any route vertex, including one at a main-point index,
        # without changing the original planning anchors. Keep that edited path.
        return self


class TemplateWrite(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    payload: TemplatePayload

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("模板名称不能为空")
        return value

    @field_validator("description")
    @classmethod
    def trim_description(cls, value: str) -> str:
        return value.strip()


class TemplateUpdate(TemplateWrite):
    expected_version: int = Field(ge=1)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RouteTemplateStore:
    def __init__(self, path: Path = DEFAULT_DB_PATH):
        self.path = path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS route_templates (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version >= 1),
                    payload_json TEXT NOT NULL
                )
            """)
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _payload_json(payload: TemplatePayload) -> str:
        raw = payload.model_dump_json()
        if len(raw.encode("utf-8")) > MAX_TEMPLATE_BYTES:
            raise TemplateTooLarge("模板数据超过 8 MB 上限")
        return raw

    @staticmethod
    def _record(row: sqlite3.Row, include_payload: bool = True) -> dict:
        result = {key: row[key] for key in ("id", "name", "description", "created_at", "updated_at", "version")}
        if include_payload:
            result["payload"] = json.loads(row["payload_json"])
        else:
            anchors = json.loads(row["payload_json"])["plan_input"]["anchors"]
            result["anchor_count"] = len(anchors)
            result["start"] = {key: anchors[0][key] for key in ("latitude", "longitude")}
            result["end"] = {key: anchors[-1][key] for key in ("latitude", "longitude")}
        return result

    def list(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM route_templates ORDER BY updated_at DESC, id DESC").fetchall()
        return [self._record(row, include_payload=False) for row in rows]

    def get(self, template_id: str) -> dict:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM route_templates WHERE id = ?", (template_id,)).fetchone()
        if row is None:
            raise TemplateNotFound()
        return self._record(row)

    def create(self, request: TemplateWrite) -> dict:
        template_id = str(uuid4())
        now = _utc_now()
        raw = self._payload_json(request.payload)
        try:
            with self._connection() as connection:
                connection.execute("""
                    INSERT INTO route_templates (id, name, description, created_at, updated_at, version, payload_json)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                """, (template_id, request.name, request.description, now, now, raw))
        except sqlite3.IntegrityError as error:
            raise TemplateConflict("模板名称已存在") from error
        return self.get(template_id)

    def update(self, template_id: str, request: TemplateUpdate) -> dict:
        now = _utc_now()
        raw = self._payload_json(request.payload)
        try:
            with self._connection() as connection:
                cursor = connection.execute("""
                    UPDATE route_templates
                    SET name = ?, description = ?, updated_at = ?, version = version + 1, payload_json = ?
                    WHERE id = ? AND version = ?
                """, (request.name, request.description, now, raw, template_id, request.expected_version))
                if cursor.rowcount == 0:
                    row = connection.execute("SELECT version FROM route_templates WHERE id = ?", (template_id,)).fetchone()
                    if row is None:
                        raise TemplateNotFound()
                    raise TemplateConflict("模板已被其他操作修改，请重新加载后再保存")
        except sqlite3.IntegrityError as error:
            raise TemplateConflict("模板名称已存在") from error
        return self.get(template_id)

    def delete(self, template_id: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM route_templates WHERE id = ?", (template_id,))
            if cursor.rowcount == 0:
                raise TemplateNotFound()
