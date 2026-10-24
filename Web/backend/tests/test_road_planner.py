import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from requests import Timeout
from shapely.geometry import LineString
from shapely.ops import unary_union

from main import app, road_network
from online_router import _fetch_route
from road_planner import (
    LOCAL_INDEX_FILE, MAX_SNAP_M, METRES_PER_DEGREE_LAT,
    METRES_PER_DEGREE_LON, CampusRoadNetwork, to_lon_lat, to_xy,
)
from auth_test_support import login_admin


class CampusRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        login_admin(self.client)
        self.request = {
            "start": {"latitude": 31.67920, "longitude": 119.95150},
            "end": {"latitude": 31.68040, "longitude": 119.94950},
            "altitude_m": 100,
            "speed_kmh": 45,
            "clearance_m": 2,
        }

    def test_default_route_uses_campus_roads_and_avoids_buildings(self):
        response = self.client.post("/api/routes/plan", json=self.request)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["road_names"], ["创业大道", "天泽路"])
        self.assertGreater(result["distance_km"], result["straight_distance_km"])
        self.assertGreater(len(result["geometry"]["coordinates"]), 4)
        route = LineString([to_xy(latitude, longitude) for longitude, latitude in result["geometry"]["coordinates"]])
        buildings = unary_union([building.buffer(2) for building in road_network().buildings])
        self.assertFalse(route.intersects(buildings))

    def test_distant_point_does_not_fall_back_to_straight_line(self):
        self.request["end"] = {"latitude": 31.5, "longitude": 119.5}
        response = self.client.post("/api/routes/plan", json=self.request)
        self.assertEqual(response.status_code, 422)
        self.assertIn("公里", response.json()["detail"])

    @unittest.skipUnless(LOCAL_INDEX_FILE.is_file(), "江苏 PBF 索引尚未建立")
    def test_external_point_uses_local_roads_without_long_connector(self):
        self.request["start"] = {"latitude": 31.6808, "longitude": 119.954}
        self.request["end"] = {"latitude": 31.6798, "longitude": 119.948}
        response = self.client.post("/api/routes/plan", json=self.request)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertIn("jiangsu-260916.osm.pbf", result["data_source"])
        self.assertLessEqual(result["start_snap_m"], MAX_SNAP_M)
        self.assertLessEqual(result["end_snap_m"], MAX_SNAP_M)
        self.assertGreater(len(result["geometry"]["coordinates"]), 4)
        self.assertGreater(result["distance_m"], result["straight_distance_m"])

    @unittest.skipUnless(LOCAL_INDEX_FILE.is_file(), "江苏 PBF 索引尚未建立")
    def test_point_check_distinguishes_building_open_area_and_nearby_road(self):
        building = road_network().buildings[5].representative_point()
        building_lon, building_lat = to_lon_lat(building.x, building.y)
        cases = [
            ((building_lat, building_lon), "mapped_building"),
            ((31.6802, 119.9505), "no_road"),
            ((31.6804, 119.9495), "road_nearby"),
        ]
        for (latitude, longitude), expected in cases:
            with self.subTest(expected=expected):
                response = self.client.post("/api/landing/check", json={
                    "point": {"latitude": latitude, "longitude": longitude},
                    "clearance_m": 2,
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["state"], expected)
                self.assertFalse(response.json()["landing_verified"])

    @unittest.skipUnless(LOCAL_INDEX_FILE.is_file(), "江苏 PBF 索引尚未建立")
    def test_offroad_point_is_rejected_without_a_straight_connector(self):
        self.request["end"] = {"latitude": 31.6802, "longitude": 119.9505}
        response = self.client.post("/api/routes/plan", json=self.request)
        self.assertEqual(response.status_code, 422)
        self.assertIn("35 米", response.json()["detail"])

    def test_search_radius_can_choose_reachable_road_over_closer_isolated_road(self):
        latitude, longitude = 31.6798, 119.9505

        def lon_lat(x: float, y: float) -> tuple[float, float]:
            return longitude + x / METRES_PER_DEGREE_LON, latitude + y / METRES_PER_DEGREE_LAT

        isolated_a, isolated_b = lon_lat(1, -5), lon_lat(1, 5)
        connected_a, connected_b = lon_lat(20, 0), lon_lat(20, 100)
        roads = [
            (1, 1, 2, *isolated_a, *isolated_b, 1, "孤立道路", "service"),
            (2, 3, 4, *connected_a, *connected_b, 2, "可达道路", "service"),
        ]
        network = CampusRoadNetwork.from_index_rows(roads, [], latitude, longitude, "test")
        result = network.plan(latitude, longitude, connected_b[1], connected_b[0], 0)
        self.assertAlmostEqual(result["start_snap_m"], 20, delta=0.2)
        self.assertEqual(result["road_names"], ["可达道路"])

    def test_allowed_segments_cache_reuses_building_buffer_for_same_clearance(self):
        latitude, longitude = 31.6798, 119.9505
        roads = [
            (1, 1, 2, longitude, latitude, longitude + 0.0001, latitude, 1, "测试道路", "service"),
        ]
        network = CampusRoadNetwork.from_index_rows(roads, [], latitude, longitude, "test")
        with patch("road_planner.unary_union", wraps=unary_union) as merge:
            first = network._allowed_segments(2)
            second = network._allowed_segments(2)
        self.assertIs(first, second)
        self.assertEqual(merge.call_count, 1)

    def test_online_source_uses_online_geometry_and_labels_unchecked_clearance(self):
        self.request["road_source"] = "online"
        payload = {
            "code": "Ok",
            "waypoints": [{"distance": 10.0}, {"distance": 4.0}],
            "routes": [{
                "distance": 300.0,
                "geometry": {"coordinates": [[119.9514, 31.6792], [119.9505, 31.6795], [119.9495, 31.6804]]},
                "legs": [{"steps": [{"name": "测试道路"}]}],
            }],
        }
        _fetch_route.cache_clear()
        with patch("online_router.requests.get") as get:
            get.return_value.json.return_value = payload
            response = self.client.post("/api/routes/plan", json=self.request)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["route_type"], "online_driving_reference")
        self.assertEqual(result["building_clearance_m"], None)
        self.assertEqual(result["geometry"]["coordinates"][0], [119.9515, 31.6792])
        self.assertEqual(result["geometry"]["coordinates"][-1], [119.9495, 31.6804])
        self.assertIn("未检查建筑缓冲", result["risk"]["reason"])
        self.assertEqual(result["road_names"], ["测试道路"])
        get.assert_called_once()

    def test_online_failure_does_not_fall_back_to_local(self):
        self.request["road_source"] = "online"
        _fetch_route.cache_clear()
        with patch("online_router.requests.get", side_effect=Timeout):
            response = self.client.post("/api/routes/plan", json=self.request)
        self.assertEqual(response.status_code, 503)
        self.assertIn("在线道路服务暂时不可用", response.json()["detail"])

    def test_online_source_rejects_long_straight_connector(self):
        self.request["road_source"] = "online"
        payload = {
            "code": "Ok",
            "waypoints": [{"distance": 190.0}, {"distance": 4.0}],
            "routes": [{"distance": 300.0, "geometry": {"coordinates": [[119.9514, 31.6792], [119.9495, 31.6804]]}}],
        }
        _fetch_route.cache_clear()
        with patch("online_router.requests.get") as get:
            get.return_value.json.return_value = payload
            response = self.client.post("/api/routes/plan", json=self.request)
        self.assertEqual(response.status_code, 422)
        self.assertIn("超过 35 米", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
