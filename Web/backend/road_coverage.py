"""Road-following area coverage routes using an undirected rural-postman approximation."""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from shapely.geometry import GeometryCollection, LineString, MultiLineString, Point, Polygon

from road_planner import ROAD_KINDS, RouteUnavailable, length

MAX_COVERAGE_EDGES = 12_000
MAX_COVERAGE_VERTICES = 10_000
MAX_COVERAGE_AREA_M2 = 80_000_000


@dataclass(frozen=True)
class CoverageEdge:
    edge_id: str
    a: str
    b: str
    coordinates: tuple[tuple[float, float], tuple[float, float]]
    distance_m: float
    required: bool
    name: str | None
    highway: str | None


def _line_parts(geometry: object) -> Iterable[LineString]:
    if isinstance(geometry, LineString):
        yield geometry
    elif isinstance(geometry, (MultiLineString, GeometryCollection)):
        for child in geometry.geoms:
            yield from _line_parts(child)


def _shortest_path(
    adjacency: dict[str, list[tuple[str, int]]],
    edges: list[CoverageEdge],
    sources: set[str],
    targets: set[str],
) -> list[int]:
    frontier: list[tuple[float, str]] = []
    costs: dict[str, float] = {}
    previous: dict[str, tuple[str, int]] = {}
    for source in sources:
        costs[source] = 0.0
        heapq.heappush(frontier, (0.0, source))
    target: str | None = None
    while frontier:
        cost, node = heapq.heappop(frontier)
        if cost != costs.get(node):
            continue
        if node in targets:
            target = node
            break
        for neighbor, edge_index in adjacency.get(node, []):
            next_cost = cost + edges[edge_index].distance_m
            if next_cost < costs.get(neighbor, math.inf):
                costs[neighbor] = next_cost
                previous[neighbor] = (node, edge_index)
                heapq.heappush(frontier, (next_cost, neighbor))
    if target is None:
        raise RouteUnavailable("区域内的目标道路分成互不连通的路网，无法沿道路连接。")
    path: list[int] = []
    node = target
    while node not in sources:
        parent, edge_index = previous[node]
        path.append(edge_index)
        node = parent
    path.reverse()
    return path


def _shortest_paths_from(
    adjacency: dict[str, list[tuple[str, int]]], edges: list[CoverageEdge], source: str
) -> tuple[dict[str, float], dict[str, tuple[str, int]]]:
    frontier: list[tuple[float, str]] = [(0.0, source)]
    costs = {source: 0.0}
    previous: dict[str, tuple[str, int]] = {}
    while frontier:
        cost, node = heapq.heappop(frontier)
        if cost != costs.get(node):
            continue
        for neighbor, edge_index in adjacency.get(node, []):
            next_cost = cost + edges[edge_index].distance_m
            if next_cost < costs.get(neighbor, math.inf):
                costs[neighbor] = next_cost
                previous[neighbor] = (node, edge_index)
                heapq.heappush(frontier, (next_cost, neighbor))
    return costs, previous


def _restore_path(previous: dict[str, tuple[str, int]], source: str, target: str) -> list[int]:
    path: list[int] = []
    node = target
    while node != source:
        parent, edge_index = previous[node]
        path.append(edge_index)
        node = parent
    path.reverse()
    return path


def _pair_odd_nodes(
    nodes: set[str], adjacency: dict[str, list[tuple[str, int]]], edges: list[CoverageEdge]
) -> list[int]:
    ordered = sorted(nodes)
    if len(ordered) <= 16:
        path_tree = {node: _shortest_paths_from(adjacency, edges, node) for node in ordered}
        full_mask = (1 << len(ordered)) - 1

        @lru_cache(maxsize=None)
        def minimum_pairing(mask: int) -> tuple[float, tuple[tuple[int, int], ...]]:
            if not mask:
                return 0.0, ()
            first_bit = mask & -mask
            first = first_bit.bit_length() - 1
            remaining = mask ^ first_bit
            best_cost = math.inf
            best_pairs: tuple[tuple[int, int], ...] = ()
            candidates = remaining
            while candidates:
                second_bit = candidates & -candidates
                second = second_bit.bit_length() - 1
                candidates ^= second_bit
                rest_cost, rest_pairs = minimum_pairing(remaining ^ second_bit)
                pair_cost = path_tree[ordered[first]][0].get(ordered[second], math.inf)
                total = pair_cost + rest_cost
                if total < best_cost:
                    best_cost = total
                    best_pairs = ((first, second), *rest_pairs)
            return best_cost, best_pairs

        cost, pairs = minimum_pairing(full_mask)
        if not math.isfinite(cost):
            raise RouteUnavailable("道路路网未连通，无法补齐巡查闭环。")
        result: list[int] = []
        for first, second in pairs:
            previous = path_tree[ordered[first]][1]
            result.extend(_restore_path(previous, ordered[first], ordered[second]))
        return result

    # Large intersections sets use nearest pairing to keep request time bounded.
    unmatched = set(ordered)
    result = []
    while unmatched:
        source = min(unmatched)
        costs, previous = _shortest_paths_from(adjacency, edges, source)
        candidates = unmatched - {source}
        target = min(candidates, key=lambda node: costs.get(node, math.inf))
        if not math.isfinite(costs.get(target, math.inf)):
            raise RouteUnavailable("道路路网未连通，无法补齐巡查闭环。")
        result.extend(_restore_path(previous, source, target))
        unmatched.remove(source)
        unmatched.remove(target)
    return result


def _components(edges: list[CoverageEdge], start: str) -> list[set[str]]:
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.a, set()).add(edge.b)
        adjacency.setdefault(edge.b, set()).add(edge.a)
    components: list[set[str]] = []
    unseen = set(adjacency)
    while unseen:
        seed = start if start in unseen else min(unseen)
        component = {seed}
        stack = [seed]
        unseen.remove(seed)
        while stack:
            node = stack.pop()
            for neighbor in adjacency.get(node, set()):
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    component.add(neighbor)
                    stack.append(neighbor)
        components.append(component)
    return components


def _euler_trail(edges: list[CoverageEdge], start: str) -> list[tuple[CoverageEdge, str, str]]:
    adjacency: dict[str, list[int]] = {}
    for index, edge in enumerate(edges):
        adjacency.setdefault(edge.a, []).append(index)
        adjacency.setdefault(edge.b, []).append(index)
    used = [False] * len(edges)
    cursors = {node: 0 for node in adjacency}
    nodes = [start]
    path_edges: list[int] = []
    reverse_trail: list[int] = []
    while nodes:
        node = nodes[-1]
        cursor = cursors[node]
        incident = adjacency[node]
        while cursor < len(incident) and used[incident[cursor]]:
            cursor += 1
        cursors[node] = cursor
        if cursor == len(incident):
            nodes.pop()
            if path_edges:
                reverse_trail.append(path_edges.pop())
            continue
        edge_index = incident[cursor]
        cursors[node] = cursor + 1
        if used[edge_index]:
            continue
        used[edge_index] = True
        edge = edges[edge_index]
        next_node = edge.b if edge.a == node else edge.a
        nodes.append(next_node)
        path_edges.append(edge_index)
    trail = list(reversed(reverse_trail))
    if len(trail) != len(edges):
        raise RouteUnavailable("道路覆盖路线没有形成连续闭环，请缩小选区或调整道路等级。")
    oriented: list[tuple[CoverageEdge, str, str]] = []
    current = start
    for edge_index in trail:
        edge = edges[edge_index]
        next_node = edge.b if edge.a == current else edge.a
        oriented.append((edge, current, next_node))
        current = next_node
    if current != start:
        raise RouteUnavailable("道路覆盖路线无法闭合，请检查该区域的道路连通情况。")
    return oriented


def plan_area_coverage(network, polygon_coordinates: list[tuple[float, float]], road_classes: set[str]) -> dict:
    if not road_classes or not road_classes.issubset(ROAD_KINDS):
        raise RouteUnavailable("请选择有效的 OSM 道路等级。")
    if len(polygon_coordinates) < 3 or len(polygon_coordinates) > 128:
        raise RouteUnavailable("区域边界需要 3–128 个顶点。")
    polygon_xy = [network._to_xy(latitude, longitude) for longitude, latitude in polygon_coordinates]
    polygon = Polygon(polygon_xy)
    if not polygon.is_valid or polygon.is_empty or polygon.area <= 1:
        raise RouteUnavailable("框选区域无效；请调整边界，避免交叉或面积过小。")
    if polygon.area > MAX_COVERAGE_AREA_M2:
        raise RouteUnavailable("框选区域超过 80 平方公里，请缩小范围后重试。")

    positions: dict[str, tuple[float, float]] = dict(network.positions)
    all_edges: list[CoverageEdge] = []
    required_edges: list[CoverageEdge] = []
    for segment_index, segment in enumerate(network.segments):
        a = positions.get(segment.a)
        b = positions.get(segment.b)
        if a is None or b is None or length(a, b) <= 0.01:
            continue
        source = LineString((a, b))
        cut_fractions = {0.0, 1.0}
        intersection = source.intersection(polygon)
        for part in _line_parts(intersection):
            if len(part.coords) < 2:
                continue
            for coordinate in (part.coords[0], part.coords[-1]):
                cut_fractions.add(max(0.0, min(1.0, source.project(Point(coordinate), normalized=True))))
        cuts = sorted(cut_fractions)
        for part_index, (start_fraction, end_fraction) in enumerate(zip(cuts, cuts[1:])):
            if end_fraction - start_fraction <= 1e-9:
                continue
            start_xy = source.interpolate(start_fraction, normalized=True).coords[0]
            end_xy = source.interpolate(end_fraction, normalized=True).coords[0]
            distance = length(start_xy, end_xy)
            if distance <= 0.05:
                continue

            def node_id(fraction: float) -> str:
                if fraction <= 1e-8:
                    return segment.a
                if fraction >= 1 - 1e-8:
                    return segment.b
                return f"cut:{segment_index}:{fraction:.10f}"

            start_id = node_id(start_fraction)
            end_id = node_id(end_fraction)
            positions[start_id] = (float(start_xy[0]), float(start_xy[1]))
            positions[end_id] = (float(end_xy[0]), float(end_xy[1]))
            midpoint = source.interpolate((start_fraction + end_fraction) / 2, normalized=True)
            required = segment.highway in road_classes and polygon.covers(midpoint)
            edge = CoverageEdge(
                f"road:{segment_index}:{part_index}", start_id, end_id,
                ((float(start_xy[0]), float(start_xy[1])), (float(end_xy[0]), float(end_xy[1]))),
                distance, required, segment.name, segment.highway,
            )
            all_edges.append(edge)
            if required:
                required_edges.append(edge)

    if not required_edges:
        raise RouteUnavailable("选区内没有找到所选道路等级的道路中心线，请扩大选区或调整筛选等级。")
    if len(required_edges) > MAX_COVERAGE_EDGES:
        raise RouteUnavailable(f"选区道路数量超过 {MAX_COVERAGE_EDGES} 段，请缩小范围。")

    road_adjacency: dict[str, list[tuple[str, int]]] = {}
    for edge_index, edge in enumerate(all_edges):
        road_adjacency.setdefault(edge.a, []).append((edge.b, edge_index))
        road_adjacency.setdefault(edge.b, []).append((edge.a, edge_index))

    required_nodes = {node for edge in required_edges for node in (edge.a, edge.b)}
    center = polygon.centroid
    start_node = min(required_nodes, key=lambda node: math.hypot(positions[node][0] - center.x, positions[node][1] - center.y))
    work_edges = list(required_edges)

    # Join disconnected selected-road components with the shortest paths found
    # on the complete local road graph. These connector roads are tracked separately.
    while True:
        components = _components(work_edges, start_node)
        if len(components) <= 1:
            break
        root_component = next(component for component in components if start_node in component)
        remaining = set().union(*(component for component in components if component is not root_component))
        connector_path = _shortest_path(road_adjacency, all_edges, root_component, remaining)
        work_edges.extend(all_edges[index] for index in connector_path)

    degree: dict[str, int] = {}
    for edge in work_edges:
        degree[edge.a] = degree.get(edge.a, 0) + 1
        degree[edge.b] = degree.get(edge.b, 0) + 1
    unmatched = {node for node, value in degree.items() if value % 2}
    work_edges.extend(all_edges[index] for index in _pair_odd_nodes(unmatched, road_adjacency, all_edges))

    trail = _euler_trail(work_edges, start_node)
    route_xy: list[tuple[float, float]] = []
    road_names: list[str] = []
    total_distance = 0.0
    traversed_required = 0.0
    connector_distance = 0.0
    for edge, from_node, _to_node in trail:
        coordinates = edge.coordinates if from_node == edge.a else tuple(reversed(edge.coordinates))
        for coordinate in coordinates:
            if not route_xy or length(route_xy[-1], coordinate) > 0.01:
                route_xy.append(coordinate)
        total_distance += edge.distance_m
        if edge.required:
            traversed_required += edge.distance_m
        else:
            connector_distance += edge.distance_m
        if edge.name and edge.name not in road_names:
            road_names.append(edge.name)

    if len(route_xy) > MAX_COVERAGE_VERTICES:
        raise RouteUnavailable(f"规划路线超过 {MAX_COVERAGE_VERTICES} 个航点，请缩小框选范围。")
    if len(route_xy) < 2:
        raise RouteUnavailable("选区内没有足够的道路节点生成路线。")
    start_lon_lat = network._to_lon_lat(*route_xy[0])
    end_lon_lat = network._to_lon_lat(*route_xy[-1])
    route_coordinates = [network._to_lon_lat(*point) for point in route_xy]
    required_distance = sum(edge.distance_m for edge in required_edges)
    unique_names = road_names
    return {
        "geometry": {"type": "LineString", "coordinates": route_coordinates},
        "distance_m": round(total_distance, 1),
        "straight_distance_m": 0.0,
        "start_snap_m": 0.0,
        "end_snap_m": 0.0,
        "road_names": unique_names,
        "building_clearance_m": None,
        "data_source": network.data_source,
        "anchors": [
            {"id": "area-start", "latitude": start_lon_lat[1], "longitude": start_lon_lat[0]},
            {"id": "area-end", "latitude": end_lon_lat[1], "longitude": end_lon_lat[0]},
        ],
        "anchor_indices": [0, len(route_coordinates) - 1],
        "segments": [{
            "id": "area-coverage", "index": 0, "from_id": "area-start", "to_id": "area-end",
            "start_index": 0, "end_index": len(route_coordinates) - 1,
            "distance_m": round(total_distance, 1), "distance_km": round(total_distance / 1000, 3),
            "estimated_minutes": 0, "road_names": unique_names,
            "start_snap_m": 0.0, "end_snap_m": 0.0,
        }],
        "area_coverage": {
            "road_classes": sorted(road_classes),
            "target_road_m": round(required_distance, 1),
            "repeated_road_m": round(max(0.0, traversed_required - required_distance), 1),
            "connector_m": round(connector_distance, 1),
            "coverage_ratio": 1.0,
            "road_edge_count": len(required_edges),
        },
    }
