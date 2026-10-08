"""First-stage route workspace API.

Weather and route optimization are deliberately separate. This API never labels a
route safe until a time- and height-matched weather grid is available.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from online_router import OnlineServiceUnavailable, plan_online_route
from road_planner import CampusRoadNetwork, LOCAL_INDEX_FILE, LocalRoadStore, RouteUnavailable
from weather_service import weather_store
from live_weather import forecast_jobs


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
    altitude_m: int = Field(default=100, ge=0, le=1000)
    speed_kmh: float = Field(default=45, gt=0, le=200)
    clearance_m: float = Field(default=2.0, ge=0, le=20)


class LandingCheckRequest(BaseModel):
    point: Point
    clearance_m: float = Field(default=2.0, ge=0, le=20)


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
