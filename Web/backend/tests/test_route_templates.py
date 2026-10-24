import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from main import app
from route_templates import RouteTemplateStore
from auth_test_support import login_admin


def example_payload():
    anchors = [
        {"id": "anchor-1", "latitude": 31.6792, "longitude": 119.9515},
        {"id": "anchor-2", "latitude": 31.6804, "longitude": 119.9495},
    ]
    coordinates = [[119.9515, 31.6792], [119.9501, 31.6797], [119.9495, 31.6804]]
    return {
        "schema_version": 1,
        "plan_input": {
            "anchors": anchors,
            "road_source": "local",
            "altitude_m": 100,
            "speed_kmh": 45,
            "clearance_m": 2,
        },
        "route_snapshot": {
            "geometry": {"type": "LineString", "coordinates": coordinates},
            "distance_m": 420,
            "distance_km": 0.42,
            "straight_distance_km": 0.23,
            "estimated_minutes": 0.56,
            "altitude_m": 100,
            "road_names": ["测试道路"],
            "start_snap_m": 0,
            "end_snap_m": 0,
            "building_clearance_m": 2,
            "data_source": "test",
            "risk": {"level": "unassessed", "score": None, "reason": "测试"},
            "anchors": anchors,
            "anchor_indices": [0, 2],
            "segments": [{"id": "segment-1", "start_index": 0, "end_index": 2}],
            "route_type": "road_corridor_demo",
        },
        "waypoint_settings": [
            {"altitude_m": 100, "heading_mode": "followWayline", "heading_angle": 0,
             "gimbal_pitch_angle": 0, "export_anchor": True},
            {"altitude_m": 120, "heading_mode": "fixed", "heading_angle": 145,
             "gimbal_pitch_angle": -45, "export_anchor": True},
            {"altitude_m": 100, "heading_mode": "followWayline", "heading_angle": 0,
             "gimbal_pitch_angle": 0, "export_anchor": True},
        ],
        "dji_model": "m30",
    }


class RouteTemplateTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parents[1] / "user_data" / f"test-{uuid4()}.sqlite"
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.store_patch = patch("main.route_template_store", return_value=RouteTemplateStore(self.path))
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.client = TestClient(app)
        login_admin(self.client)

    def _create(self, name="演示航线"):
        return self.client.post("/api/route-templates", json={
            "name": name, "description": "一号路线", "payload": example_payload(),
        })

    def test_round_trip_preserves_full_geometry_and_waypoint_view(self):
        created = self._create()
        self.assertEqual(created.status_code, 201, created.text)
        record = created.json()
        self.assertEqual(record["name"], "演示航线")
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["payload"]["route_snapshot"]["geometry"]["coordinates"][1],
                         [119.9501, 31.6797])
        self.assertEqual(record["payload"]["waypoint_settings"][1]["gimbal_pitch_angle"], -45)
        self.assertEqual(record["payload"]["route_snapshot"]["route_type"], "road_corridor_demo")
        self.assertTrue(self.path.is_file())

        listed = self.client.get("/api/route-templates").json()["templates"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("payload", listed[0])
        self.assertEqual(listed[0]["anchor_count"], 2)
        self.assertEqual(listed[0]["start"], {"latitude": 31.6792, "longitude": 119.9515})
        self.assertEqual(listed[0]["end"], {"latitude": 31.6804, "longitude": 119.9495})
        reopened = RouteTemplateStore(self.path).get(record["id"])
        self.assertEqual(reopened, record)
        self.assertNotIn("departure_time", str(reopened["payload"]))
        self.assertNotIn("weather", str(reopened["payload"]))

    def test_separate_templates_update_conflict_and_delete(self):
        first = self._create("路线一").json()
        second = self._create("路线二").json()
        self.assertNotEqual(first["id"], second["id"])
        revised = example_payload()
        revised["waypoint_settings"][1]["heading_angle"] = 270
        updated = self.client.put(f"/api/route-templates/{first['id']}", json={
            "name": "路线一新版", "description": "修改后的路线", "payload": revised,
            "expected_version": first["version"],
        })
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["version"], 2)
        self.assertEqual(updated.json()["payload"]["waypoint_settings"][1]["heading_angle"], 270)
        stale = self.client.put(f"/api/route-templates/{first['id']}", json={
            "name": "旧版本", "payload": example_payload(), "expected_version": 1,
        })
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.client.get(f"/api/route-templates/{second['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/route-templates/{first['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/route-templates/{first['id']}").status_code, 404)
        self.assertEqual(len(self.client.get("/api/route-templates").json()["templates"]), 1)

    def test_summary_uses_first_and_last_of_multiple_anchors(self):
        payload = example_payload()
        middle = {"id": "anchor-middle", "latitude": 31.6797, "longitude": 119.9501}
        anchors = [payload["plan_input"]["anchors"][0], middle, payload["plan_input"]["anchors"][1]]
        payload["plan_input"]["anchors"] = anchors
        payload["route_snapshot"]["anchors"] = anchors
        payload["route_snapshot"]["anchor_indices"] = [0, 1, 2]
        created = self.client.post("/api/route-templates", json={"name": "三主点", "payload": payload})
        self.assertEqual(created.status_code, 201, created.text)
        summary = self.client.get("/api/route-templates").json()["templates"][0]
        self.assertEqual(summary["anchor_count"], 3)
        self.assertEqual(summary["start"], {"latitude": 31.6792, "longitude": 119.9515})
        self.assertEqual(summary["end"], {"latitude": 31.6804, "longitude": 119.9495})

    def test_moved_main_point_vertex_is_preserved_without_changing_planning_anchors(self):
        payload = example_payload()
        moved = [119.9518, 31.6794]
        payload["route_snapshot"]["geometry"]["coordinates"][0] = moved
        created = self.client.post("/api/route-templates", json={"name": "微调后的航线", "payload": payload})
        self.assertEqual(created.status_code, 201, created.text)
        saved = self.client.get(f"/api/route-templates/{created.json()['id']}").json()["payload"]
        self.assertEqual(saved["route_snapshot"]["geometry"]["coordinates"][0], moved)
        self.assertEqual(saved["plan_input"]["anchors"][0], {
            "id": "anchor-1", "latitude": 31.6792, "longitude": 119.9515,
        })

    def test_zero_height_route_matches_existing_form_range(self):
        payload = example_payload()
        payload["plan_input"]["altitude_m"] = 0
        payload["route_snapshot"]["altitude_m"] = 0
        for setting in payload["waypoint_settings"]:
            setting["altitude_m"] = 0
        created = self.client.post("/api/route-templates", json={"name": "零米演示", "payload": payload})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["payload"]["waypoint_settings"][0]["altitude_m"], 0)

    def test_bad_geometry_indices_settings_and_names_are_rejected(self):
        payload = example_payload()
        cases = [
            ("empty name", " ", payload),
            ("too long name", "x" * 81, payload),
            ("missing settings", "Valid", {**payload, "waypoint_settings": payload["waypoint_settings"][:2]}),
            ("bad indices", "Valid", {**payload, "route_snapshot": {
                **payload["route_snapshot"], "anchor_indices": [0, 1],
            }}),
            ("bad coordinate", "Valid", {**payload, "route_snapshot": {
                **payload["route_snapshot"], "geometry": {
                    "type": "LineString", "coordinates": [[119.9515, 31.6792], [119.9501, 91], [119.9495, 31.6804]],
                },
            }}),
            ("bad schema", "Valid", {**payload, "schema_version": 2}),
        ]
        for label, name, candidate in cases:
            with self.subTest(label=label):
                response = self.client.post("/api/route-templates", json={"name": name, "payload": candidate})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/api/route-templates").json(), {"templates": []})

    def test_duplicate_name_and_missing_record(self):
        self.assertEqual(self._create("Same").status_code, 201)
        self.assertEqual(self._create("same").status_code, 409)
        missing = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(self.client.get(f"/api/route-templates/{missing}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/route-templates/{missing}").status_code, 404)
        self.assertEqual(self.client.put(f"/api/route-templates/{missing}", json={
            "name": "Missing", "payload": example_payload(), "expected_version": 1,
        }).status_code, 404)

    def test_same_origin_mutations_and_no_cors_grants(self):
        # CORS 中间件已移除：同源部署（8011 托管前端 / 5173 代理）不颁发跨域许可，
        # 预检请求不再被特判，改写模板靠登录会话直接请求。
        body = {"name": "SameOrigin", "payload": example_payload(), "expected_version": 1}
        created = self.client.post("/api/route-templates", json=body).json()
        self.assertEqual(self.client.put(f"/api/route-templates/{created['id']}", json={
            **body, "expected_version": created["version"],
        }).status_code, 200)
        response = self.client.options(f"/api/route-templates/{created['id']}", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "PUT",
        })
        self.assertEqual(response.status_code, 405)
        self.assertNotIn("access-control-allow-methods", response.headers)


if __name__ == "__main__":
    unittest.main()
