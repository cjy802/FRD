"""First-stage route workspace API.

Weather screening and road comparison never grant flight permission. Aircraft,
airspace, 3D obstacles and unmodeled hazards remain independent checks.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator

from online_router import OnlineServiceUnavailable, plan_online_route, plan_online_alternatives
from road_planner import CampusRoadNetwork, LOCAL_INDEX_FILE, LocalRoadStore, ROAD_KINDS, RouteUnavailable
from weather_service import weather_store
from live_weather import forecast_jobs
from route_candidates import local_road_candidates
from weather_routes import compare_road_weather
from dji_kmz import KmzExportError, build_dji_kmz
from dji_rc2 import Rc2Error, rc2_imports
from place_search import PlaceSearchError, reverse_geocode, search_nearby_places, search_places
from photo_analysis import PhotoAnalysisError, photo_analysis
from video_survey import VideoSurveyError, video_surveys
from mission_routes import MAX_MISSION_ANCHORS, MissionRouteError, MissionRouteLimitError, combine_mission_segments
from route_templates import (
    RouteTemplateStore, TemplateConflict, TemplateNotFound, TemplateTooLarge,
    TemplateUpdate, TemplateWrite,
)
from task_store import TaskError, tasks
from adaptive_video import AdaptiveVideoError, select_frames as select_adaptive_video_frames


app = FastAPI(title="低空飞行决策工作台", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)


class Point(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RouteRequest(BaseModel):
    start: Point
    end: Point
    road_source: Literal["local", "online"] = "local"
    altitude_m: float = Field(default=100, ge=0, le=1000, allow_inf_nan=False)
    speed_kmh: float = Field(default=45, gt=0, le=200)
    clearance_m: float = Field(default=2.0, ge=0, le=20)


class MissionAnchor(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


class MissionRouteRequest(BaseModel):
    anchors: list[MissionAnchor] = Field(min_length=2, max_length=MAX_MISSION_ANCHORS)
    road_source: Literal["local", "online"] = "local"
    altitude_m: float = Field(default=100, ge=0, le=1000, allow_inf_nan=False)
    speed_kmh: float = Field(default=45, gt=0, le=200, allow_inf_nan=False)
    clearance_m: float = Field(default=2.0, ge=0, le=20, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_anchors(self):
        ids = [anchor.id for anchor in self.anchors]
        if any(not value.strip() for value in ids):
            raise ValueError("主点 ID 不能为空。")
        if len(ids) != len(set(ids)):
            raise ValueError("主点 ID 必须唯一。")
        for index, (start, end) in enumerate(zip(self.anchors, self.anchors[1:]), start=1):
            if start.latitude == end.latitude and start.longitude == end.longitude:
                raise ValueError(f"第 {index} 段的相邻主点不能重合。")
        return self


class AreaCoverageRequest(BaseModel):
    polygon: list[Point] = Field(min_length=3, max_length=128)
    road_classes: list[str] = Field(
        default_factory=lambda: ["motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified", "service"],
        min_length=1,
        max_length=10,
    )
    start: Point | None = None
    end: Point | None = None
    endpoint_snap_m: float = Field(default=100, ge=1, le=500, allow_inf_nan=False)
    altitude_m: float = Field(default=100, ge=0, le=1000, allow_inf_nan=False)
    speed_kmh: float = Field(default=45, gt=0, le=200, allow_inf_nan=False)

    @field_validator("road_classes")
    @classmethod
    def validate_road_classes(cls, values: list[str]) -> list[str]:
        selected = list(dict.fromkeys(value.strip() for value in values))
        if not selected or any(value not in ROAD_KINDS for value in selected):
            raise ValueError("道路等级筛选项无效。")
        return selected

    @model_validator(mode="after")
    def validate_polygon(self):
        unique = {(round(point.latitude, 7), round(point.longitude, 7)) for point in self.polygon}
        if len(unique) < 3:
            raise ValueError("区域边界至少需要三个不同的顶点。")
        if (self.start is None) != (self.end is None):
            raise ValueError("区域起点和终点需要同时设置。")
        return self


class LandingCheckRequest(BaseModel):
    point: Point
    clearance_m: float = Field(default=2.0, ge=0, le=20)


class WeatherLimits(BaseModel):
    name: str = Field(default='自定义阈值', max_length=80)
    max_wind_m_s: float = Field(gt=0, le=200, allow_inf_nan=False)
    max_crosswind_m_s: float = Field(gt=0, le=200, allow_inf_nan=False)
    max_headwind_m_s: float = Field(gt=0, le=200, allow_inf_nan=False)
    max_gust_m_s: float = Field(gt=0, le=200, allow_inf_nan=False)
    max_rain_6h_mm: float = Field(gt=0, le=500, allow_inf_nan=False)
    min_visibility_m: float = Field(gt=0, le=100000, allow_inf_nan=False)


class RouteWeatherRequest(BaseModel):
    coordinates: list[tuple[float, float]] = Field(min_length=2, max_length=10000)
    departure_time_utc: str
    speed_kmh: float = Field(default=45, gt=0, le=200, allow_inf_nan=False)
    altitude_m: float = Field(default=100, ge=0, le=1000, allow_inf_nan=False)
    profile: WeatherLimits | None = None


class WeatherRouteComparisonRequest(RouteRequest):
    weather_job_id: str
    departure_time_utc: str
    profile: WeatherLimits | None = None


class KmzWaypointSetting(BaseModel):
    altitude_m: float | None = Field(default=None, gt=0, le=1500, allow_inf_nan=False)
    heading_mode: Literal["followWayline", "manually", "fixed", "smoothTransition", "towardPOI"] = "followWayline"
    heading_angle: float = Field(default=0, allow_inf_nan=False)
    gimbal_pitch_angle: float = Field(default=0, ge=-90, le=45, allow_inf_nan=False)
    export_anchor: bool = False
    poi: tuple[float, float, float] | None = None


class KmzExportRequest(BaseModel):
    coordinates: list[tuple[float, float]] = Field(min_length=2, max_length=65535)
    altitude_m: float = Field(gt=0, le=1500, allow_inf_nan=False)
    speed_kmh: float = Field(gt=0, le=200, allow_inf_nan=False)
    drone_model: Literal["fly", "m30", "m3e"] = "fly"
    waypoint_settings: list[KmzWaypointSetting] | None = Field(default=None, max_length=65535)


class Rc2ImportRequest(KmzExportRequest):
    drone_model: Literal["fly"] = "fly"
    target_file_name: str = Field(min_length=5, max_length=128)
    target_folder: str | None = Field(default=None, min_length=36, max_length=36)


class VideoCaptureTargetRequest(BaseModel):
    time_seconds: float = Field(ge=0, allow_inf_nan=False)
    label: str = Field(default="", max_length=60)


class AdaptiveFrameSelectionRequest(BaseModel):
    survey_id: str = Field(min_length=23, max_length=23)
    straight_overlap_percent: float = Field(default=12, ge=0, le=50, allow_inf_nan=False)
    turn_overlap_percent: float = Field(default=60, ge=20, le=90, allow_inf_nan=False)
    scan_fps: float = Field(default=2, gt=0, le=5, allow_inf_nan=False)
    turn_angle_degrees: float = Field(default=18, ge=10, le=90, allow_inf_nan=False)
    camera_change_degrees: float = Field(default=12, ge=5, le=45, allow_inf_nan=False)
    blur_filter_enabled: bool = True
    minimum_sharpness_percent: float = Field(default=40, ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_overlap_targets(self):
        if self.turn_overlap_percent <= self.straight_overlap_percent:
            raise ValueError("转弯重叠目标应高于直线段目标。")
        return self


class TaskCreateRequest(BaseModel):
    route: dict[str, Any]
    departure_time: str = Field(min_length=1, max_length=40)
    departure_auto_managed: bool = True
    name: str = Field(min_length=1, max_length=80)
    weather: dict[str, Any] | None = None
    aircraft: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("请填写任务名称。")
        return normalized


class TaskUpdateRequest(BaseModel):
    status: Literal["planning", "ready", "in_flight", "completed"] | None = None
    weather: dict[str, Any] | None = None
    aircraft: dict[str, Any] | None = None
    export_file_name: str | None = Field(default=None, max_length=128)
    flight_started_at: str | None = Field(default=None, max_length=40)
    video_survey_id: str | None = Field(default=None, max_length=40)
    analysis_job_id: str | None = Field(default=None, max_length=40)
    analysis_targets: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    departure_time: str | None = Field(default=None, min_length=1, max_length=40)
    departure_auto_managed: bool | None = None


@lru_cache(maxsize=1)
def road_network() -> CampusRoadNetwork:
    return CampusRoadNetwork()


@lru_cache(maxsize=1)
def local_road_store() -> LocalRoadStore:
    return LocalRoadStore()


def distance_km(a: Point, b: Point) -> float:
    r = 6371.0088
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(b.longitude - a.longitude)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(h)))


@app.get("/api/health")
def health():
    return {"status": "ok", "road_data": "jiangsu_pbf_index" if LOCAL_INDEX_FILE.is_file() else "campus_osm"}


@app.post("/api/tasks", status_code=status.HTTP_201_CREATED)
def create_task(request: Request, payload: TaskCreateRequest):
    require_local_request(request, mutation=True, label="飞行任务")
    try:
        return tasks.create(payload.route, payload.departure_time, payload.departure_auto_managed,
                            payload.name, payload.weather, payload.aircraft)
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error


@app.get("/api/tasks")
def list_tasks(request: Request):
    require_local_request(request, label="任务列表")
    return {"tasks": tasks.list_summaries()}


@app.get("/api/tasks/{task_id}")
def get_task(request: Request, task_id: str):
    require_local_request(request, label="飞行任务")
    try:
        return tasks.get(task_id)
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error


@app.delete("/api/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(request: Request, task_id: str):
    require_local_request(request, mutation=True, label="飞行任务")
    try:
        tasks.delete(task_id)
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.patch("/api/tasks/{task_id}")
def update_task(request: Request, task_id: str, payload: TaskUpdateRequest):
    require_local_request(request, mutation=True, label="飞行任务")
    try:
        return tasks.update(task_id, payload.model_dump(exclude_unset=True))
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error


@app.get("/api/tasks/{task_id}/frames")
def list_task_frames(request: Request, task_id: str):
    require_local_request(request, label="任务关键帧")
    try:
        return {"frames": tasks.frames(task_id)}
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/frames", status_code=status.HTTP_201_CREATED)
async def save_task_frame(
    request: Request,
    task_id: str,
    frame: UploadFile = File(...),
    time_seconds: float = Form(ge=0),
    latitude: float | None = Form(default=None, ge=-90, le=90),
    longitude: float | None = Form(default=None, ge=-180, le=180),
    altitude_m: float | None = Form(default=None),
    gimbal_pitch_deg: float | None = Form(default=None, ge=-90, le=45),
    selection_reason: str = Form(default="", max_length=40),
):
    require_local_request(request, mutation=True, label="任务关键帧")
    try:
        return tasks.add_frame(task_id, frame.filename or "frame.jpg", await frame.read(),
                               {"time_seconds": time_seconds, "latitude": latitude,
                                "longitude": longitude, "altitude_m": altitude_m,
                                "gimbal_pitch_deg": gimbal_pitch_deg,
                                "selection_reason": selection_reason.strip() or None})
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error


@app.get("/api/tasks/{task_id}/frames/{frame_id}")
def get_task_frame(request: Request, task_id: str, frame_id: str):
    require_local_request(request, label="任务关键帧")
    try:
        path = tasks.frame_path(task_id, frame_id)
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error
    media_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower())
    return FileResponse(path, media_type=media_type)


@app.delete("/api/tasks/{task_id}/frames/{frame_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task_frame(request: Request, task_id: str, frame_id: str):
    require_local_request(request, mutation=True, label="任务关键帧")
    try:
        tasks.remove_frame(task_id, frame_id)
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/tasks/{task_id}/adaptive-frames")
async def select_task_video_frames(request: Request, task_id: str, payload: AdaptiveFrameSelectionRequest):
    require_local_request(request, mutation=True, label="任务视频自适应切帧")
    try:
        task = tasks.get(task_id)
        if task.get("video_survey_id") != payload.survey_id:
            raise TaskError("请选择当前任务已导入的视频与 SRT。", http_status=409)
        survey = video_surveys.job(payload.survey_id)
        if survey.get("task_id") != task_id or not survey.get("has_gps"):
            raise VideoSurveyError("此视频没有绑定当前任务的有效 SRT GPS 轨迹。", http_status=422)
        remaining_frames = 30 - len(tasks.frames(task_id))
        if remaining_frames <= 0:
            raise TaskError("当前任务的关键帧已达 30 张上限，请先移除不需要的帧。", http_status=409)
        video_path = video_surveys.video_path(payload.survey_id)
        before_count = len(tasks.frames(task_id))
        with tempfile.TemporaryDirectory(prefix="adaptive-frame-selection-", dir=str(tasks.directory(task_id))) as temporary:
            output_dir = Path(temporary)
            selection = await asyncio.to_thread(
                select_adaptive_video_frames,
                video_path,
                survey["telemetry"],
                output_dir,
                max_frames=remaining_frames,
                scan_fps=payload.scan_fps,
                straight_overlap_percent=payload.straight_overlap_percent,
                turn_overlap_percent=payload.turn_overlap_percent,
                turn_angle_degrees=payload.turn_angle_degrees,
                camera_change_degrees=payload.camera_change_degrees,
                blur_filter_enabled=payload.blur_filter_enabled,
                minimum_sharpness_percent=payload.minimum_sharpness_percent,
            )
            stored_frames = []
            for item in selection["selected_frames"]:
                image_path = (output_dir / item["file"]).resolve()
                if output_dir.resolve() not in image_path.parents or not image_path.is_file():
                    raise AdaptiveVideoError("切帧程序输出的图像路径无效。", http_status=500)
                stored_frames.append(tasks.add_frame(
                    task_id,
                    image_path.name,
                    image_path.read_bytes(),
                    {"time_seconds": item["time_seconds"], "latitude": item["latitude"],
                     "longitude": item["longitude"], "altitude_m": item.get("altitude_m"),
                     "gimbal_pitch_deg": item.get("gimbal_pitch_deg"),
                     "selection_reason": item.get("selection_reason"),
                     "estimated_overlap_percent": item.get("estimated_overlap_percent"),
                     "turn_angle_deg": item.get("turn_angle_deg"),
                     "sharpness_score": item.get("sharpness_score"),
                     "sharpness_ratio_percent": item.get("sharpness_ratio_percent"),
                     "source_frame_index": item.get("source_frame_index"),
                     "source_video_time": item.get("time_seconds")},
                ))
        return {**{key: value for key, value in selection.items() if key != "selected_frames"},
                "task_frames": stored_frames, "frame_count_before": before_count,
                "frame_count_after": before_count + len(stored_frames)}
    except AdaptiveVideoError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error
    except VideoSurveyError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error


@lru_cache(maxsize=1)
def route_template_store() -> RouteTemplateStore:
    return RouteTemplateStore()


@app.get("/api/route-templates")
def list_route_templates():
    return {"templates": route_template_store().list()}


@app.post("/api/route-templates", status_code=status.HTTP_201_CREATED)
def create_route_template(request: TemplateWrite):
    try:
        return route_template_store().create(request)
    except TemplateConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TemplateTooLarge as error:
        raise HTTPException(status_code=413, detail=str(error)) from error


@app.get("/api/route-templates/{template_id}")
def get_route_template(template_id: str):
    try:
        return route_template_store().get(template_id)
    except TemplateNotFound:
        raise HTTPException(status_code=404, detail="模板不存在") from None


@app.put("/api/route-templates/{template_id}")
def update_route_template(template_id: str, request: TemplateUpdate):
    try:
        return route_template_store().update(template_id, request)
    except TemplateNotFound:
        raise HTTPException(status_code=404, detail="模板不存在") from None
    except TemplateConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TemplateTooLarge as error:
        raise HTTPException(status_code=413, detail=str(error)) from error


@app.delete("/api/route-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_route_template(template_id: str):
    try:
        route_template_store().delete(template_id)
    except TemplateNotFound:
        raise HTTPException(status_code=404, detail="模板不存在") from None


@app.get("/api/places/search")
def lookup_places(query: str = Query(min_length=1, max_length=128), region: str = Query(default="常州市", max_length=50)):
    try:
        return search_places(query, region)
    except PlaceSearchError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from None


@app.get("/api/places/around")
def lookup_nearby_places(
    query: str = Query(min_length=1, max_length=128),
    latitude: float = Query(ge=-90, le=90),
    longitude: float = Query(ge=-180, le=180),
):
    try:
        return search_nearby_places(query, latitude, longitude)
    except PlaceSearchError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from None


@app.get("/api/places/reverse")
def lookup_location_address(
    latitude: float = Query(ge=-90, le=90),
    longitude: float = Query(ge=-180, le=180),
):
    try:
        return reverse_geocode(latitude, longitude)
    except PlaceSearchError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from None


@app.get("/api/weather/status")
def weather_status():
    try:
        return weather_store().status()
    except (OSError, ValueError) as error:
        message = str(error)
    configured = os.getenv("WEATHER_GRID_PATH", "").strip()
    path = Path(configured) if configured else None
    exists = bool(path and path.is_file())
    return {
        "state": "file_detected" if exists else "awaiting_data",
        "source": path.name if exists and path else None,
        "ready_for_route_risk": False,
        "message": (
            f"{message} 已检测到网格文件，仍需解析时间、高度和空间覆盖。"
            if exists
            else message
        ),
        "options": [],
    }


@app.get("/api/weather/forecast")
def weather_forecast(init_time_utc: str | None = None, lead_hours: int | None = None):
    try:
        return weather_store().forecast(init_time_utc, lead_hours)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/weather/risk")
def weather_risk(init_time_utc: str | None = None, lead_hours: int | None = None):
    forecast = weather_forecast(init_time_utc, lead_hours)
    return {
        "init_time_utc": forecast["init_time_utc"],
        "valid_time_utc": forecast["valid_time_utc"],
        "lead_hours": forecast["lead_hours"],
        "point": forecast["point"],
        "sources": forecast["sources"],
        "derived": forecast["derived"],
        **forecast["point_risk"],
    }


@app.post("/api/weather/predict")
def start_weather_prediction(refresh: bool = Query(default=False)):
    return forecast_jobs.start(force_refresh=refresh)


@app.get("/api/weather/predictions/{job_id}")
def weather_prediction_job(job_id: str):
    try:
        return forecast_jobs.get(job_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="天气预测任务不存在，请重新预测。") from error


@app.get("/api/weather/predictions/{job_id}/sample")
def sample_regional_weather(job_id: str, latitude: float, longitude: float, valid_time_utc: str | None = None):
    try:
        return forecast_jobs.sample(job_id, latitude, longitude, valid_time_utc) if valid_time_utc else forecast_jobs.sample(job_id, latitude, longitude)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="天气预测任务不存在，请重新预测。") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/weather/predictions/{job_id}/series")
def sample_ground_weather_series(job_id: str, latitude: float, longitude: float, hours: int = 6):
    try:
        return forecast_jobs.series(job_id, latitude, longitude, hours)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="天气预测任务不存在，请重新预测。") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/weather/predictions/{job_id}/route")
def align_route_weather(job_id: str, request: RouteWeatherRequest):
    try:
        return forecast_jobs.route(job_id, **request.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="天气预测任务不存在，请重新预测。") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/landing/check")
def check_landing_point(request: LandingCheckRequest):
    try:
        planner = local_road_store() if LOCAL_INDEX_FILE.is_file() else road_network()
        result = planner.inspect_point(
            request.point.latitude, request.point.longitude, request.clearance_m
        )
    except RouteUnavailable as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {**result, "landing_verified": False, "data_source": planner.data_source}


@app.post('/api/weather/predictions/{job_id}/windows')
def compare_weather_windows(job_id: str, request: RouteWeatherRequest):
    try:
        return forecast_jobs.windows(job_id, **request.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail='天气预测任务不存在，请重新预测。') from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/routes/preview")
def preview_route(route: RouteRequest):
    km = distance_km(route.start, route.end)
    # This is a geometric preview, not a navigation or weather optimization result.
    return {
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [route.start.longitude, route.start.latitude],
                [route.end.longitude, route.end.latitude],
            ],
        },
        "distance_km": round(km, 2),
        "estimated_minutes": round(km / route.speed_kmh * 60, 1),
        "altitude_m": route.altitude_m,
        "risk": {"level": "unassessed", "score": None, "reason": "尚未检查沿线建筑、禁飞区和天气"},
        "optimization": {"state": "unavailable", "reason": "缺少建筑轮廓、高度、天气场和禁飞区约束"},
    }


@app.post("/api/routes/plan")
def plan_road_route(route: RouteRequest):
    try:
        if route.road_source == "online":
            result = plan_online_route(
                route.start.latitude,
                route.start.longitude,
                route.end.latitude,
                route.end.longitude,
                clearance_m=route.clearance_m,
            )
        else:
            planner = local_road_store() if LOCAL_INDEX_FILE.is_file() else road_network()
            result = planner.plan(
                route.start.latitude,
                route.start.longitude,
                route.end.latitude,
                route.end.longitude,
                clearance_m=route.clearance_m,
            )
    except OnlineServiceUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except RouteUnavailable as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return route_response(route, result)


@app.post("/api/routes/plan-mission")
def plan_mission_route(route: MissionRouteRequest):
    """Plan each user-selected anchor pair and join their generated vertices."""
    anchors = [anchor.model_dump() for anchor in route.anchors]
    planner = None
    results = []
    for index, (start, end) in enumerate(zip(route.anchors, route.anchors[1:]), start=1):
        try:
            if route.road_source == "online":
                result = plan_online_route(
                    start.latitude, start.longitude, end.latitude, end.longitude,
                    clearance_m=route.clearance_m,
                )
            else:
                if planner is None:
                    planner = local_road_store() if LOCAL_INDEX_FILE.is_file() else road_network()
                result = planner.plan(
                    start.latitude, start.longitude, end.latitude, end.longitude,
                    clearance_m=route.clearance_m,
                )
        except OnlineServiceUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail=f"第 {index} 段（{start.id}→{end.id}）规划失败：{error}",
            ) from error
        except RouteUnavailable as error:
            raise HTTPException(
                status_code=422,
                detail=f"第 {index} 段（{start.id}→{end.id}）规划失败：{error}",
            ) from error
        results.append(result)
    try:
        combined = combine_mission_segments(anchors, results, route.speed_kmh)
    except MissionRouteLimitError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except MissionRouteError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return route_response(route, combined)


@app.post("/api/routes/area-coverage")
def plan_area_coverage(route: AreaCoverageRequest):
    """Cover selected major road edges inside a drawn region using road-only connectors."""
    try:
        planner = local_road_store() if LOCAL_INDEX_FILE.is_file() else road_network()
        result = planner.plan_area_coverage(
            [(point.longitude, point.latitude) for point in route.polygon], set(route.road_classes),
            start=(route.start.latitude, route.start.longitude) if route.start else None,
            end=(route.end.latitude, route.end.longitude) if route.end else None,
            endpoint_snap_m=route.endpoint_snap_m,
        )
    except RouteUnavailable as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    distance_km = result["distance_m"] / 1000
    for segment in result["segments"]:
        segment["estimated_minutes"] = round(segment["distance_km"] / route.speed_kmh * 60, 1)
    return {
        **result,
        "distance_km": round(distance_km, 3),
        "straight_distance_km": 0,
        "estimated_minutes": round(distance_km / route.speed_kmh * 60, 1),
        "altitude_m": route.altitude_m,
        "route_type": "area_road_coverage_open",
        "risk": {
            "level": "unassessed",
            "score": None,
            "reason": "路线沿本地 OpenStreetMap 道路中心线覆盖；道路数据完整性、无人机空域、障碍和现场条件仍需核验。",
        },
    }


@app.post("/api/routes/export-kmz")
def export_dji_kmz(request: KmzExportRequest, task_id: str | None = Query(default=None, min_length=23, max_length=23)):
    try:
        settings = [item.model_dump() for item in request.waypoint_settings] if request.waypoint_settings is not None else None
        payload = build_dji_kmz(request.coordinates, request.altitude_m, request.speed_kmh, request.drone_model, settings)
        if task_id:
            directory = tasks.directory(task_id) / "exports"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "dji_waypoint_draft.kmz").write_bytes(payload)
            aircraft = tasks.get(task_id).get("aircraft") or {}
            aircraft["drone_model"] = request.drone_model
            aircraft["waypoint_settings"] = [item.model_dump() for item in request.waypoint_settings] if request.waypoint_settings is not None else []
            tasks.update(task_id, {"export_file_name": "dji_waypoint_draft.kmz", "status": "ready",
                                   "aircraft": aircraft})
    except KmzExportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error
    return Response(
        content=payload,
        media_type="application/vnd.google-earth.kmz",
        headers={"Content-Disposition": 'attachment; filename="dji_waypoint_draft.kmz"'},
    )


_RC2_ALLOWED_ORIGINS = {
    "http://localhost:5173", "http://127.0.0.1:5173",
    "http://localhost:8011", "http://127.0.0.1:8011",
}
_RC2_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def require_local_request(request: Request, *, mutation: bool = False, label: str = "本机功能") -> None:
    """Keep local-device actions on the local UI and outside cross-site requests."""
    client_host = request.client.host if request.client else None
    try:
        host_name = urlsplit(f"http://{request.headers.get('host', '')}").hostname
    except ValueError:
        host_name = None
    if client_host not in {"127.0.0.1", "::1"} or host_name not in _RC2_LOOPBACK_NAMES:
        raise HTTPException(status_code=403, detail=f"{label}只允许从本机回环地址访问。")
    origin = request.headers.get("origin")
    if (origin and origin not in _RC2_ALLOWED_ORIGINS) or (mutation and not origin):
        raise HTTPException(status_code=403, detail=f"{label}请求必须来自本机页面。")
    if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
        raise HTTPException(status_code=403, detail=f"{label}不接受跨站页面请求。")
    referer = request.headers.get("referer")
    if referer:
        parsed = urlsplit(referer)
        if f"{parsed.scheme}://{parsed.netloc}" not in _RC2_ALLOWED_ORIGINS:
            raise HTTPException(status_code=403, detail=f"{label}只接受本机页面引用。")


def require_local_rc2_request(request: Request, *, mutation: bool = False) -> None:
    """Keep MTP actions on the local UI and outside cross-site requests."""
    require_local_request(request, mutation=mutation, label="RC 2 导入")


def rc2_http_error(error: Rc2Error) -> HTTPException:
    return HTTPException(status_code=error.http_status, detail=str(error))


@app.get("/api/dji/rc2/status")
def rc2_status(request: Request):
    require_local_rc2_request(request)
    return rc2_imports.status()


@app.get("/api/dji/rc2/targets")
def rc2_targets(request: Request):
    require_local_rc2_request(request)
    try:
        return rc2_imports.targets()
    except Rc2Error as error:
        raise rc2_http_error(error) from error


@app.post("/api/dji/rc2/import", status_code=status.HTTP_202_ACCEPTED)
def rc2_import(request: Request, import_request: Rc2ImportRequest):
    require_local_rc2_request(request, mutation=True)
    try:
        settings = [item.model_dump() for item in import_request.waypoint_settings] if import_request.waypoint_settings is not None else None
        payload = build_dji_kmz(
            import_request.coordinates, import_request.altitude_m, import_request.speed_kmh,
            "fly", settings,
        )
        return rc2_imports.start(payload, import_request.target_file_name, import_request.target_folder)
    except KmzExportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Rc2Error as error:
        raise rc2_http_error(error) from error


@app.get("/api/dji/rc2/jobs/{job_id}")
def rc2_job(request: Request, job_id: str):
    require_local_rc2_request(request)
    try:
        return rc2_imports.job(job_id)
    except Rc2Error as error:
        raise rc2_http_error(error) from error


@app.get("/api/photo-analysis/health")
def photo_analysis_health(request: Request):
    require_local_request(request, label="照片分析")
    return photo_analysis.health()


@app.post("/api/photo-analysis/jobs", status_code=status.HTTP_202_ACCEPTED)
async def photo_analysis_submit(
    request: Request,
    photos: list[UploadFile] = File(...),
    site_name: str = Form(default=""),
    note: str = Form(default=""),
    model_id: str = Form(default=""),
    task_id: str = Form(default=""),
    frame_metadata: str = Form(default="[]"),
    camera_fov_deg: str = Form(default=""),
):
    require_local_request(request, mutation=True, label="照片分析")
    if not photos:
        raise HTTPException(status_code=422, detail="没有收到照片文件。")
    collected = []
    for upload in photos:
        collected.append((upload.filename or "photo", await upload.read()))
    try:
        context = {"site_name": site_name, "note": note}
        if camera_fov_deg.strip():
            try:
                fov = float(camera_fov_deg)
            except ValueError as error:
                raise PhotoAnalysisError("相机视场角必须是数字。") from error
            if not 30.0 <= fov <= 120.0:
                raise PhotoAnalysisError("相机视场角需在 30–120° 之间。")
            context["camera_fov_deg"] = fov
        try:
            parsed_frame_metadata = json.loads(frame_metadata)
        except ValueError as error:
            raise PhotoAnalysisError("视频截图位置信息格式无效。") from error
        if not isinstance(parsed_frame_metadata, list) or len(parsed_frame_metadata) != len(collected):
            raise PhotoAnalysisError("视频截图与位置信息数量不一致。")
        context["frames"] = parsed_frame_metadata
        if task_id:
            tasks.get(task_id)
        job_id = photo_analysis.submit(collected, context=context,
                                       model_id=model_id or None, task_id=task_id or None)
        if task_id:
            tasks.update(task_id, {"analysis_job_id": job_id})
    except PhotoAnalysisError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error
    return {"job_id": job_id, "job_url": f"/api/photo-analysis/jobs/{job_id}"}


@app.get("/api/photo-analysis/jobs/{job_id}")
def photo_analysis_job(request: Request, job_id: str):
    require_local_request(request, label="照片分析")
    try:
        return photo_analysis.job(job_id)
    except PhotoAnalysisError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error


_PHOTO_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".bmp": "image/bmp", ".webp": "image/webp"}


@app.get("/api/photo-analysis/jobs/{job_id}/files/{file_name:path}")
def photo_analysis_file(request: Request, job_id: str, file_name: str):
    require_local_request(request, label="照片分析")
    try:
        target = photo_analysis.output_file(job_id, file_name)
    except PhotoAnalysisError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error
    return FileResponse(target, media_type=_PHOTO_MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream"))


@app.post("/api/video-surveys", status_code=status.HTTP_201_CREATED)
async def video_survey_create(request: Request, video: UploadFile = File(...), srt: UploadFile = File(...),
                              task_id: str = Form(default="")):
    require_local_request(request, mutation=True, label="视频定位")
    try:
        if task_id:
            tasks.get(task_id)
        result = await video_surveys.create(video, srt, task_id or None)
        if task_id:
            tasks.update(task_id, {"video_survey_id": result["job_id"]})
        return result
    except VideoSurveyError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error
    except TaskError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error


@app.get("/api/video-surveys/{job_id}")
def video_survey_job(request: Request, job_id: str):
    require_local_request(request, label="视频定位")
    try:
        return video_surveys.job(job_id)
    except VideoSurveyError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error


def _file_range(path: Path, range_header: str | None):
    size = path.stat().st_size
    start, end, partial = 0, max(size - 1, 0), False
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match or (not match.group(1) and not match.group(2)):
            raise HTTPException(status_code=416, detail="不支持的视频字节范围。", headers={"Content-Range": f"bytes */{size}"})
        if match.group(1):
            start = int(match.group(1))
            end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
        else:
            suffix = int(match.group(2))
            start = max(0, size - suffix)
        if start >= size or end < start:
            raise HTTPException(status_code=416, detail="视频字节范围超出文件大小。", headers={"Content-Range": f"bytes */{size}"})
        partial = True

    def chunks():
        remaining = end - start + 1
        with path.open("rb") as stream:
            stream.seek(start)
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store"}
    status_code = 206 if partial else 200
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    media_type = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime"}.get(path.suffix.lower(), "application/octet-stream")
    return StreamingResponse(chunks(), status_code=status_code, media_type=media_type, headers=headers)


@app.get("/api/video-surveys/{job_id}/video")
def video_survey_video(request: Request, job_id: str):
    require_local_request(request, label="视频定位")
    try:
        path = video_surveys.video_path(job_id)
    except VideoSurveyError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error
    return _file_range(path, request.headers.get("range"))


@app.post("/api/video-surveys/{job_id}/targets", status_code=status.HTTP_201_CREATED)
def video_survey_add_target(request: Request, job_id: str, payload: VideoCaptureTargetRequest):
    require_local_request(request, mutation=True, label="近拍点")
    try:
        return video_surveys.add_target(job_id, payload.time_seconds, payload.label)
    except VideoSurveyError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error


@app.delete("/api/video-surveys/{job_id}/targets/{target_id}")
def video_survey_delete_target(request: Request, job_id: str, target_id: str):
    require_local_request(request, mutation=True, label="近拍点")
    try:
        return {"targets": video_surveys.remove_target(job_id, target_id)}
    except VideoSurveyError as error:
        raise HTTPException(status_code=error.http_status, detail=str(error)) from error


def route_response(route, result):
    km = result["distance_m"] / 1000
    return {
        **result,
        "distance_km": round(km, 3),
        "straight_distance_km": round(result["straight_distance_m"] / 1000, 3),
        "estimated_minutes": round(km / route.speed_kmh * 60, 1),
        "altitude_m": route.altitude_m,
        "route_type": "online_driving_reference" if route.road_source == "online" else "road_corridor_demo",
        "risk": {
            "level": "unassessed",
            "score": None,
            "reason": (
                "在线机动车道路路线未检查建筑缓冲、道路上方障碍、空域和天气"
                if route.road_source == "online"
                else "已按建筑轮廓和演示缓冲区避障；未核实道路上方障碍、空域和天气"
            ),
        },
    }


@lru_cache(maxsize=32)
def road_candidates_cached(source, start_lat, start_lon, end_lat, end_lon, clearance, data_epoch, bucket):
    del data_epoch, bucket
    if source == 'online': return plan_online_alternatives(start_lat, start_lon, end_lat, end_lon, clearance)
    planner = local_road_store() if LOCAL_INDEX_FILE.is_file() else road_network()
    return local_road_candidates(planner, start_lat, start_lon, end_lat, end_lon, clearance)


@app.post('/api/routes/compare-weather')
def compare_weather_routes(request: WeatherRouteComparisonRequest):
    try:
        grid, _, generated = forecast_jobs.regional_job(request.weather_job_id)
        epoch = LOCAL_INDEX_FILE.stat().st_mtime_ns if LOCAL_INDEX_FILE.is_file() else 0
        candidates = road_candidates_cached(request.road_source, request.start.latitude, request.start.longitude,
                                            request.end.latitude, request.end.longitude, request.clearance_m, epoch, int(time.time() // 600))
        result = compare_road_weather(candidates, grid, lambda coordinates: forecast_jobs.route(
            request.weather_job_id, coordinates, request.departure_time_utc, request.speed_kmh, request.altitude_m,
            request.profile.model_dump() if request.profile else None))
        for candidate in result['candidates']: candidate['route'] = route_response(request, candidate['route'])
        return {**result, 'job_id': request.weather_job_id, 'generated_at_utc': generated}
    except KeyError as error:
        raise HTTPException(status_code=404, detail='天气任务不存在，请重新预测。') from error
    except OnlineServiceUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
