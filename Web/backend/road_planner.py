"""Road-corridor demonstration using local OpenStreetMap road geometry.

Road centerlines are a map reference, not an authorised or verified UAV corridor.
The 2 m default building buffer is for visual prototyping only.
"""

from __future__ import annotations

import heapq
import json
import math
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry import LineString, Point as ShapePoint, Polygon
from shapely.ops import unary_union


DATA_FILE = Path(__file__).resolve().parent / "data" / "campus.osm"
ORIGIN_LAT = 31.6798
ORIGIN_LON = 119.9505
METRES_PER_DEGREE_LAT = 111_320.0
METRES_PER_DEGREE_LON = METRES_PER_DEGREE_LAT * math.cos(math.radians(ORIGIN_LAT))
ROAD_KINDS = {
    "service", "footway", "pedestrian", "path", "cycleway",
    "residential", "living_street", "unclassified", "tertiary",
    "secondary", "primary", "trunk", "motorway",
}
MAX_SNAP_M = 35.0
LOCAL_INDEX_FILE = Path(__file__).resolve().parent / "data" / "jiangsu_roads.sqlite"


class RouteUnavailable(ValueError):
    pass


class EndpointUnavailable(RouteUnavailable):
    """An A/B point cannot connect to the known road network."""


class NetworkUnavailable(RouteUnavailable):
    """The current search window has no connected route."""


def to_xy(latitude: float, longitude: float) -> tuple[float, float]:
    return ((longitude - ORIGIN_LON) * METRES_PER_DEGREE_LON, (latitude - ORIGIN_LAT) * METRES_PER_DEGREE_LAT)


def to_lon_lat(x: float, y: float) -> list[float]:
    return [ORIGIN_LON + x / METRES_PER_DEGREE_LON, ORIGIN_LAT + y / METRES_PER_DEGREE_LAT]


def length(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass(frozen=True)
class Segment:
    a: str
    b: str
    way_id: str
    name: str | None


@dataclass(frozen=True)
class Snap:
    segment: Segment
    xy: tuple[float, float]
    fraction: float
    distance_m: float


class CampusRoadNetwork:
    def __init__(self, path: Path = DATA_FILE):
        if not path.is_file():
            raise RouteUnavailable("缺少本地校园道路数据，请先运行 scripts/fetch_campus_osm.py。")
        self.origin_lat = ORIGIN_LAT
        self.origin_lon = ORIGIN_LON
        self.metres_per_degree_lon = METRES_PER_DEGREE_LON
        self.data_source = "OpenStreetMap 本地校园快照"
        root = ET.parse(path).getroot()
        self.positions = {
            node.attrib["id"]: self._to_xy(float(node.attrib["lat"]), float(node.attrib["lon"]))
            for node in root.findall("node")
        }
        self.segments: list[Segment] = []
        self.buildings: list[Polygon] = []
        for way in root.findall("way"):
            tags = {tag.attrib["k"]: tag.attrib["v"] for tag in way.findall("tag")}
            refs = [node.attrib["ref"] for node in way.findall("nd")]
            if tags.get("building") and len(refs) >= 4 and refs[0] == refs[-1]:
                polygon = Polygon([self.positions[ref] for ref in refs])
                if polygon.is_valid and not polygon.is_empty:
                    self.buildings.append(polygon)
            if tags.get("highway") in ROAD_KINDS:
                self.segments.extend(
                    Segment(a, b, way.attrib["id"], tags.get("name"))
                    for a, b in zip(refs, refs[1:])
                    if a != b
                )
        if not self.segments:
            raise RouteUnavailable("校园数据中没有可用的道路中心线。")

    @classmethod
    def from_index_rows(
        cls,
        roads: list[tuple],
        building_rings: list[list[list[float]]],
        origin_lat: float,
        origin_lon: float,
        data_source: str,
    ) -> "CampusRoadNetwork":
        network = cls.__new__(cls)
        network.origin_lat = origin_lat
        network.origin_lon = origin_lon
        network.metres_per_degree_lon = METRES_PER_DEGREE_LAT * math.cos(math.radians(origin_lat))
        network.data_source = data_source
        network.positions = {}
        network.segments = []
        network.buildings = []
        for _, a, b, a_lon, a_lat, b_lon, b_lat, way_id, name, _ in roads:
            a_id, b_id = str(a), str(b)
            network.positions[a_id] = network._to_xy(a_lat, a_lon)
            network.positions[b_id] = network._to_xy(b_lat, b_lon)
            network.segments.append(Segment(a_id, b_id, str(way_id), name))
        for ring in building_rings:
            polygon = Polygon([network._to_xy(lat, lon) for lon, lat in ring])
            if polygon.is_valid and not polygon.is_empty:
                network.buildings.append(polygon)
        if not network.segments:
            raise NetworkUnavailable("所选区域没有已收录的道路，请扩大数据范围或重新选择点位。")
        return network

    def _to_xy(self, latitude: float, longitude: float) -> tuple[float, float]:
        return (
            (longitude - self.origin_lon) * self.metres_per_degree_lon,
            (latitude - self.origin_lat) * METRES_PER_DEGREE_LAT,
        )

    def _to_lon_lat(self, x: float, y: float) -> list[float]:
        return [self.origin_lon + x / self.metres_per_degree_lon, self.origin_lat + y / METRES_PER_DEGREE_LAT]

    def _allowed_segments(self, clearance_m: float) -> tuple[list[Segment], object]:
        obstacles = unary_union([building.buffer(clearance_m) for building in self.buildings])
        allowed = [
            segment for segment in self.segments
            if not LineString((self.positions[segment.a], self.positions[segment.b])).intersects(obstacles)
        ]
        return allowed, obstacles

    def _snap_candidates(
        self, xy: tuple[float, float], segments: list[Segment], obstacles: object, endpoint_name: str
    ) -> list[Snap]:
        if ShapePoint(xy).intersects(obstacles):
            raise EndpointUnavailable(f"{endpoint_name}落在已收录建筑或建筑缓冲区内，不能作为候选起降点。")
        candidates: list[Snap] = []
        nearest_distance = math.inf
        blocked_nearby = False
        for segment in segments:
            a, b = self.positions[segment.a], self.positions[segment.b]
            dx, dy = b[0] - a[0], b[1] - a[1]
            squared = dx * dx + dy * dy
            if squared == 0:
                continue
            fraction = max(0.0, min(1.0, ((xy[0] - a[0]) * dx + (xy[1] - a[1]) * dy) / squared))
            projected = (a[0] + fraction * dx, a[1] + fraction * dy)
            distance_m = length(xy, projected)
            nearest_distance = min(nearest_distance, distance_m)
            if distance_m > MAX_SNAP_M:
                continue
            connector = LineString((xy, projected))
            if connector.intersects(obstacles):
                blocked_nearby = True
                continue
            candidates.append(Snap(segment, projected, fraction, distance_m))
        if not candidates:
            if blocked_nearby:
                raise EndpointUnavailable(f"{endpoint_name}附近道路被已收录建筑阻断，请调整点位。")
            if math.isfinite(nearest_distance):
                raise EndpointUnavailable(
                    f"{endpoint_name}距最近可用道路 {nearest_distance:.1f} 米，超过 {MAX_SNAP_M:.0f} 米接路搜索半径。"
                )
            raise EndpointUnavailable(f"{endpoint_name}附近没有已收录的可用道路。")
        return candidates

    def inspect_point(self, latitude: float, longitude: float, clearance_m: float = 2.0) -> dict:
        """Small XML fallback for the point-check endpoint."""
        allowed, obstacles = self._allowed_segments(clearance_m)
        xy = self._to_xy(latitude, longitude)
        if ShapePoint(xy).intersects(obstacles):
            return {
                "state": "mapped_building", "search_radius_m": MAX_SNAP_M,
                "road_distance_m": None, "road_point": None,
                "message": "该点位于已收录建筑或建筑缓冲区内，不能作为候选起降点。",
            }
        try:
            best = min(self._snap_candidates(xy, allowed, obstacles, "点位"), key=lambda item: item.distance_m)
        except EndpointUnavailable:
            return {
                "state": "no_road", "search_radius_m": MAX_SNAP_M,
                "road_distance_m": None, "road_point": None,
                "message": f"未落在已收录建筑内，但 {MAX_SNAP_M:.0f} 米接路范围内没有可连接道路；落地条件仍需现场核验。",
            }
        return {
            "state": "road_nearby", "search_radius_m": MAX_SNAP_M,
            "road_distance_m": round(best.distance_m, 1), "road_point": self._to_lon_lat(*best.xy),
            "message": "已找到接路范围内的道路；该点仅是候选落点，仍需核验地面和空中障碍。",
        }

    def plan(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
        clearance_m: float = 2.0,
    ) -> dict:
        allowed, obstacles = self._allowed_segments(clearance_m)
        if not allowed:
            raise NetworkUnavailable("当前建筑缓冲距离下没有可用道路。")
        start_xy = self._to_xy(start_lat, start_lon)
        end_xy = self._to_xy(end_lat, end_lon)
        start_candidates = self._snap_candidates(start_xy, allowed, obstacles, "起点 A")
        end_candidates = self._snap_candidates(end_xy, allowed, obstacles, "终点 B")

        positions = {**self.positions, "__start__": start_xy, "__end__": end_xy}
        graph: dict[str, list[tuple[str, float, str | None]]] = {}
        snap_by_id: dict[str, Snap] = {}
        start_snap_ids: list[str] = []
        end_snap_ids: list[str] = []

        def add_edge(a: str, b: str, road_name: str | None, metres: float | None = None) -> None:
            distance = length(positions[a], positions[b]) if metres is None else metres
            graph.setdefault(a, []).append((b, distance, road_name))
            graph.setdefault(b, []).append((a, distance, road_name))

        for segment in allowed:
            add_edge(segment.a, segment.b, segment.name)
        for endpoint_id, candidates, snap_ids in (
            ("__start__", start_candidates, start_snap_ids),
            ("__end__", end_candidates, end_snap_ids),
        ):
            for index, snap in enumerate(candidates):
                snap_id = f"{endpoint_id}_snap_{index}"
                positions[snap_id] = snap.xy
                snap_by_id[snap_id] = snap
                snap_ids.append(snap_id)
                add_edge(endpoint_id, snap_id, None, snap.distance_m)
                add_edge(snap_id, snap.segment.a, snap.segment.name)
                add_edge(snap_id, snap.segment.b, snap.segment.name)
        for start_id in start_snap_ids:
            for end_id in end_snap_ids:
                if snap_by_id[start_id].segment == snap_by_id[end_id].segment:
                    add_edge(start_id, end_id, snap_by_id[start_id].segment.name)

        frontier: list[tuple[float, str]] = [(0.0, "__start__")]
        costs = {"__start__": 0.0}
        previous: dict[str, tuple[str, str | None]] = {}
        while frontier:
            cost, current = heapq.heappop(frontier)
            if current == "__end__":
                break
            if cost > costs[current]:
                continue
            for next_node, edge_cost, road_name in graph.get(current, []):
                next_cost = cost + edge_cost
                if next_cost < costs.get(next_node, math.inf):
                    costs[next_node] = next_cost
                    previous[next_node] = (current, road_name)
                    heapq.heappush(frontier, (next_cost, next_node))
        if "__end__" not in costs:
            raise NetworkUnavailable("道路网络未连通，无法在当前范围内生成道路路线。")

        path = ["__end__"]
        road_names: list[str] = []
        while path[-1] != "__start__":
            parent, name = previous[path[-1]]
            if name and name not in road_names:
                road_names.append(name)
            path.append(parent)
        path.reverse()
        road_names.reverse()
        points = [positions[node] for node in path]
        compact: list[tuple[float, float]] = []
        for point in points:
            if not compact or length(compact[-1], point) > 0.01:
                compact.append(point)
        route_line = LineString(compact)
        if route_line.intersects(obstacles):
            raise RouteUnavailable("道路路线与建筑缓冲区冲突，请调整端点或缓冲距离。")
        return {
            "geometry": {"type": "LineString", "coordinates": [self._to_lon_lat(*point) for point in compact]},
            "distance_m": round(route_line.length, 1),
            "straight_distance_m": round(length(start_xy, end_xy), 1),
            "start_snap_m": round(snap_by_id[path[1]].distance_m, 1),
            "end_snap_m": round(snap_by_id[path[-2]].distance_m, 1),
            "road_names": road_names,
            "building_clearance_m": clearance_m,
            "data_source": self.data_source,
        }


class LocalRoadStore:
    """Read a small A/B area from a one-time SQLite index of the Jiangsu PBF."""

    def __init__(self, path: Path = LOCAL_INDEX_FILE):
        if not path.is_file():
            raise RouteUnavailable("缺少江苏道路索引，请运行 python scripts/index_pbf.py。")
        self.path = path
        with closing(sqlite3.connect(path)) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        self.data_source = f"OpenStreetMap 江苏本地数据（{metadata.get('source_file', path.name)}）"

    def inspect_point(
        self, latitude: float, longitude: float, clearance_m: float = 2.0,
        radius_m: float = MAX_SNAP_M,
    ) -> dict:
        """Quickly classify a candidate point using only nearby indexed features."""
        metres_per_degree_lon = METRES_PER_DEGREE_LAT * math.cos(math.radians(latitude))
        search_m = radius_m + clearance_m + 1.0
        west = longitude - search_m / metres_per_degree_lon
        east = longitude + search_m / metres_per_degree_lon
        south = latitude - search_m / METRES_PER_DEGREE_LAT
        north = latitude + search_m / METRES_PER_DEGREE_LAT
        with closing(sqlite3.connect(self.path)) as connection:
            road_rows = connection.execute("""
                SELECT r.id, r.a, r.b, r.a_lon, r.a_lat, r.b_lon, r.b_lat, r.way_id, r.name, r.highway
                FROM road_rtree AS x JOIN roads AS r ON r.id = x.id
                WHERE x.max_lon >= ? AND x.min_lon <= ? AND x.max_lat >= ? AND x.min_lat <= ?
            """, (west, east, south, north)).fetchall()
            building_rows = connection.execute("""
                SELECT b.coordinates
                FROM building_rtree AS x JOIN buildings AS b ON b.id = x.id
                WHERE x.max_lon >= ? AND x.min_lon <= ? AND x.max_lat >= ? AND x.min_lat <= ?
            """, (west, east, south, north)).fetchall()

        def xy(lon: float, lat: float) -> tuple[float, float]:
            return ((lon - longitude) * metres_per_degree_lon, (lat - latitude) * METRES_PER_DEGREE_LAT)

        buildings = []
        for row in building_rows:
            polygon = Polygon([xy(lon, lat) for lon, lat in json.loads(row[0])])
            if polygon.is_valid and not polygon.is_empty:
                buildings.append(polygon)
        obstacles = unary_union([building.buffer(clearance_m) for building in buildings])
        point = ShapePoint(0, 0)
        if point.intersects(obstacles):
            return {
                "state": "mapped_building",
                "search_radius_m": radius_m,
                "road_distance_m": None,
                "road_point": None,
                "message": "该点位于已收录建筑或建筑缓冲区内，不能作为候选起降点。",
            }

        best_distance = math.inf
        best_point: tuple[float, float] | None = None
        for _, _, _, a_lon, a_lat, b_lon, b_lat, _, _, _ in road_rows:
            road = LineString((xy(a_lon, a_lat), xy(b_lon, b_lat)))
            if road.is_empty or road.length == 0 or road.intersects(obstacles):
                continue
            projected = road.interpolate(road.project(point))
            distance_m = point.distance(projected)
            if distance_m > radius_m or distance_m >= best_distance:
                continue
            if LineString((point, projected)).intersects(obstacles):
                continue
            best_distance = distance_m
            best_point = (projected.x, projected.y)

        if best_point is None:
            return {
                "state": "no_road",
                "search_radius_m": radius_m,
                "road_distance_m": None,
                "road_point": None,
                "message": f"未落在已收录建筑内，但 {radius_m:.0f} 米接路范围内没有可连接道路；落地条件仍需现场核验。",
            }
        return {
            "state": "road_nearby",
            "search_radius_m": radius_m,
            "road_distance_m": round(best_distance, 1),
            "road_point": [
                longitude + best_point[0] / metres_per_degree_lon,
                latitude + best_point[1] / METRES_PER_DEGREE_LAT,
            ],
            "message": "已找到接路范围内的道路；该点仅是候选落点，仍需核验地面和空中障碍。",
        }

    def _network_for_area(
        self, start_lat: float, start_lon: float, end_lat: float, end_lon: float, padding_m: float
    ) -> CampusRoadNetwork:
        center_lat = (start_lat + end_lat) / 2
        center_lon = (start_lon + end_lon) / 2
        lat_padding = padding_m / METRES_PER_DEGREE_LAT
        lon_padding = padding_m / (METRES_PER_DEGREE_LAT * math.cos(math.radians(center_lat)))
        west, east = min(start_lon, end_lon) - lon_padding, max(start_lon, end_lon) + lon_padding
        south, north = min(start_lat, end_lat) - lat_padding, max(start_lat, end_lat) + lat_padding

        with closing(sqlite3.connect(self.path)) as connection:
            roads = connection.execute("""
                SELECT r.id, r.a, r.b, r.a_lon, r.a_lat, r.b_lon, r.b_lat, r.way_id, r.name, r.highway
                FROM road_rtree AS x JOIN roads AS r ON r.id = x.id
                WHERE x.max_lon >= ? AND x.min_lon <= ? AND x.max_lat >= ? AND x.min_lat <= ?
            """, (west, east, south, north)).fetchall()
            buildings = connection.execute("""
                SELECT b.coordinates
                FROM building_rtree AS x JOIN buildings AS b ON b.id = x.id
                WHERE x.max_lon >= ? AND x.min_lon <= ? AND x.max_lat >= ? AND x.min_lat <= ?
            """, (west, east, south, north)).fetchall()

        return CampusRoadNetwork.from_index_rows(
            roads, [json.loads(row[0]) for row in buildings], center_lat, center_lon, self.data_source
        )

    def plan(
        self, start_lat: float, start_lon: float, end_lat: float, end_lon: float, clearance_m: float = 2.0
    ) -> dict:
        approximate_distance = math.hypot(
            (end_lat - start_lat) * METRES_PER_DEGREE_LAT,
            (end_lon - start_lon) * METRES_PER_DEGREE_LAT * math.cos(math.radians((start_lat + end_lat) / 2)),
        )
        if approximate_distance > 10_000:
            raise RouteUnavailable("当前原型只支持两点相距 10 公里以内的本地道路规划。")

        for endpoint_name, latitude, longitude in (
            ("起点 A", start_lat, start_lon),
            ("终点 B", end_lat, end_lon),
        ):
            inspection = self.inspect_point(latitude, longitude, clearance_m)
            if inspection["state"] == "mapped_building":
                raise EndpointUnavailable(f"{endpoint_name}位于已收录建筑或建筑缓冲区内，不能作为候选起降点。")
            if inspection["state"] == "no_road":
                raise EndpointUnavailable(
                    f"{endpoint_name}周围 {MAX_SNAP_M:.0f} 米内没有可连接道路，请调整点位。"
                )

        first_padding = max(750.0, min(2_000.0, approximate_distance * 0.3))
        last_error: RouteUnavailable | None = None
        for padding in (first_padding, max(2_500.0, first_padding * 2)):
            try:
                network = self._network_for_area(start_lat, start_lon, end_lat, end_lon, padding)
                return network.plan(start_lat, start_lon, end_lat, end_lon, clearance_m)
            except NetworkUnavailable as error:
                last_error = error
        raise last_error or RouteUnavailable("本地道路网络未找到路线。")
