"""Diversify existing road routes; never generate unverified straight shortcuts."""

import math

from shapely.geometry import LineString

from road_planner import RouteUnavailable


def local_road_candidates(planner, start_lat, start_lon, end_lat, end_lon, clearance_m):
    baseline = planner.plan(start_lat, start_lon, end_lat, end_lon, clearance_m)
    coordinates = baseline['geometry']['coordinates']
    center_lat = (start_lat + end_lat) / 2
    lon_scale = 111320 * math.cos(math.radians(center_lat))
    def xy(coordinate): return ((coordinate[0] - start_lon) * lon_scale, (coordinate[1] - start_lat) * 111320)
    reference = LineString([xy(coordinate) for coordinate in coordinates])
    def penalty(a, b):
        # Geometry-only diversity, not an assertion of weather improvement.
        segment = LineString([xy(a), xy(b)])
        return 20. if segment.interpolate(.5, normalized=True).distance(reference) < 15 else 1.
    candidates = [baseline]
    try:
        alternative = planner.plan(start_lat, start_lon, end_lat, end_lon, clearance_m, edge_penalty=penalty)
        if alternative['geometry']['coordinates'] != coordinates and alternative['distance_m'] <= baseline['distance_m'] * 2:
            candidates.append(alternative)
    except RouteUnavailable:
        pass  # The baseline remains valid; no fabricated alternative geometry.
    return candidates
