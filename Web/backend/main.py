"""First-stage route workspace API.

Weather screening and road comparison never grant flight permission. Aircraft,
airspace, 3D obstacles and unmodeled hazards remain independent checks.
"""

from __future__ import annotations

import math
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from online_router import OnlineServiceUnavailable, plan_online_route, plan_online_alternatives
from road_planner import CampusRoadNetwork, LOCAL_INDEX_FILE, LocalRoadStore, RouteUnavailable
from weather_service import weather_store
from live_weather import forecast_jobs
from route_candidates import local_road_candidates
from weather_routes import compare_road_weather
from dji_kmz import KmzExportError, build_dji_kmz


app = FastAPI(title="低空飞行决策工作台", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
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
    heading_angle: float = Field(default=0, ge=-180, le=180, allow_inf_nan=False)
    gimbal_pitch_angle: float = Field(default=0, ge=-90, le=45, allow_inf_nan=False)
    poi: tuple[float, float, float] | None = None


class KmzExportRequest(BaseModel):
    coordinates: list[tuple[float, float]] = Field(min_length=2, max_length=65535)
    altitude_m: float = Field(gt=0, le=1500, allow_inf_nan=False)
    speed_kmh: float = Field(gt=0, le=200, allow_inf_nan=False)
    drone_model: Literal["fly", "m30", "m3e"] = "fly"
    waypoint_settings: list[KmzWaypointSetting] | None = Field(default=None, max_length=65535)


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
def start_weather_prediction():
    return forecast_jobs.start()


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


@app.post("/api/routes/export-kmz")
def export_dji_kmz(request: KmzExportRequest):
    try:
        settings = [item.model_dump() for item in request.waypoint_settings] if request.waypoint_settings is not None else None
        payload = build_dji_kmz(request.coordinates, request.altitude_m, request.speed_kmh, request.drone_model, settings)
    except KmzExportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return Response(
        content=payload,
        media_type="application/vnd.google-earth.kmz",
        headers={"Content-Disposition": 'attachment; filename="dji_waypoint_draft.kmz"'},
    )


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
