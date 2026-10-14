import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import MissionRouteRequest, app
from mission_routes import MAX_MISSION_ANCHORS, MAX_MISSION_VERTICES
from online_router import OnlineServiceUnavailable
from road_planner import RouteUnavailable


class MissionRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.anchors = [
            {"id": "A", "latitude": 31.6792, "longitude": 119.9515},
            {"id": "B", "latitude": 31.6804, "longitude": 119.9495},
            {"id": "C", "latitude": 31.6792, "longitude": 119.9515},
        ]

    def test_local_segments_join_once_at_manual_anchor_and_keep_generated_vertices(self):
        response = self.client.post("/api/routes/plan-mission", json={"anchors": self.anchors})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        coordinates = result["geometry"]["coordinates"]
        indices = result["anchor_indices"]
        self.assertEqual([coordinates[index] for index in indices], [
            [anchor["longitude"], anchor["latitude"]] for anchor in self.anchors
        ])
        self.assertEqual(indices, [0, result["segments"][0]["end_index"], len(coordinates) - 1])
        self.assertEqual(result["segments"][1]["start_index"], indices[1])
        self.assertNotEqual(coordinates[indices[1] - 1], coordinates[indices[1]])
        self.assertNotEqual(coordinates[indices[1]], coordinates[indices[1] + 1])
        self.assertGreater(indices[1], 1)
        self.assertGreater(indices[2] - indices[1], 1)
        self.assertEqual([(item["from_id"], item["to_id"]) for item in result["segments"]],
                         [("A", "B"), ("B", "C")])
        self.assertAlmostEqual(result["distance_m"],
                               sum(segment["distance_m"] for segment in result["segments"]), places=1)
        self.assertEqual(result["altitude_m"], 100)
        self.assertEqual(result["risk"]["level"], "unassessed")

    def test_invalid_anchor_lists_are_rejected_before_planning(self):
        cases = [
            self.anchors[:1],
            [{**self.anchors[0]}, {**self.anchors[0], "id": "B"}],
            [{**self.anchors[0]}, {**self.anchors[1], "id": "A"}],
            [{**self.anchors[0]}, {**self.anchors[1], "latitude": 91}],
        ]
        with patch("main.road_network") as planner:
            for anchors in cases:
                with self.subTest(anchors=len(anchors)):
                    response = self.client.post("/api/routes/plan-mission", json={"anchors": anchors})
                    self.assertEqual(response.status_code, 422)
            planner.assert_not_called()

    def test_planning_anchor_budget_is_independent_of_export_waypoint_limits(self):
        anchors = [
            {"id": str(index), "latitude": 31.6 + index * 0.00001, "longitude": 119.9}
            for index in range(MAX_MISSION_ANCHORS + 1)
        ]
        self.assertEqual(len(MissionRouteRequest.model_validate({"anchors": anchors[:-1]}).anchors),
                         MAX_MISSION_ANCHORS)
        with self.assertRaises(ValidationError):
            MissionRouteRequest.model_validate({"anchors": anchors})
        with patch("main.road_network") as planner:
            response = self.client.post("/api/routes/plan-mission", json={"anchors": anchors})
            planner.assert_not_called()
        self.assertEqual(response.status_code, 422)
        self.assertIn(str(MAX_MISSION_ANCHORS), response.text)

    def test_second_segment_failure_names_the_pair_without_a_straight_fallback(self):
        with patch("main.plan_online_route") as plan:
            plan.side_effect = [
                self._route(self.anchors[0], self.anchors[1]),
                RouteUnavailable("没有可用道路"),
            ]
            response = self.client.post("/api/routes/plan-mission", json={
                "anchors": self.anchors, "road_source": "online",
            })
        self.assertEqual(response.status_code, 422)
        self.assertIn("第 2 段（B→C）", response.json()["detail"])
        self.assertEqual(plan.call_count, 2)
        self.assertNotIn("geometry", response.json())

    def test_online_outage_preserves_service_error_and_segment_context(self):
        with patch("main.plan_online_route", side_effect=OnlineServiceUnavailable("服务不可用")):
            response = self.client.post("/api/routes/plan-mission", json={
                "anchors": self.anchors, "road_source": "online",
            })
        self.assertEqual(response.status_code, 503)
        self.assertIn("第 1 段（A→B）", response.json()["detail"])

    def test_disconnected_planner_geometry_is_rejected_without_an_implicit_connector(self):
        bad_second = self._route(self.anchors[1], self.anchors[2])
        bad_second["geometry"]["coordinates"][0] = [119.95, 31.6804]
        with patch("main.plan_online_route", side_effect=[
            self._route(self.anchors[0], self.anchors[1]), bad_second,
        ]):
            response = self.client.post("/api/routes/plan-mission", json={
                "anchors": self.anchors, "road_source": "online",
            })
        self.assertEqual(response.status_code, 502)
        self.assertIn("第 2 段规划结果未连接所选主点", response.json()["detail"])

    def test_excess_generated_vertices_are_reported_instead_of_dropped(self):
        points = [[119.9515, 31.6792]]
        points.extend([[119.9515 + index * 1e-8, 31.6792] for index in range(1, MAX_MISSION_VERTICES)])
        points.append([119.9495, 31.6804])
        with patch("main.plan_online_route", return_value={
            **self._route(self.anchors[0], self.anchors[1]),
            "geometry": {"type": "LineString", "coordinates": points},
        }):
            response = self.client.post("/api/routes/plan-mission", json={
                "anchors": self.anchors[:2], "road_source": "online",
            })
        self.assertEqual(response.status_code, 422)
        self.assertIn(str(MAX_MISSION_VERTICES), response.json()["detail"])

    @staticmethod
    def _route(start, end):
        return {
            "geometry": {"type": "LineString", "coordinates": [
                [start["longitude"], start["latitude"]],
                [end["longitude"], end["latitude"]],
            ]},
            "distance_m": 400,
            "straight_distance_m": 230,
            "start_snap_m": 0,
            "end_snap_m": 0,
            "road_names": ["测试道路"],
            "building_clearance_m": None,
            "data_source": "test",
        }


if __name__ == "__main__":
    unittest.main()
