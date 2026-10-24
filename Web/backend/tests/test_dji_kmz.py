import unittest
from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree

from dji_kmz import KmzExportError, _simplify_coordinate_indices, _simplify_coordinates, build_dji_kmz

SPEC_NAMESPACE = "http://www.dji.com/wpmz/1.0.2"
FLY_NAMESPACE = "http://www.uav.com/wpmz/1.0.2"


def namespace_for(profile: str) -> dict[str, str]:
    return {"wpml": FLY_NAMESPACE if profile == "fly" else SPEC_NAMESPACE, "kml": "http://www.opengis.net/kml/2.2"}


class DjiKmzTests(unittest.TestCase):
    def test_simplifies_dense_geometry_but_keeps_endpoints(self):
        coordinates = [
            [119.9500000, 31.6800000],
            [119.9500010, 31.6800010],
            [119.9500200, 31.6800200],
            [119.9501000, 31.6801000],
            [119.9501005, 31.6801005],
            [119.9502000, 31.6801000],
        ]
        simplified = _simplify_coordinates([(float(lon), float(lat)) for lon, lat in coordinates])
        self.assertLess(len(simplified), len(coordinates))
        self.assertEqual(simplified[0], tuple(coordinates[0]))
        self.assertEqual(simplified[-1], tuple(coordinates[-1]))

    def test_builds_wpml_archive_with_matching_waypoints(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.951, 31.672], [119.952, 31.672]], 80, 36, "m30")
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(sorted(archive.namelist()), ["wpmz/template.kml", "wpmz/waylines.wpml"])
            template = ElementTree.fromstring(archive.read("wpmz/template.kml"))
            waylines = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
        namespace = namespace_for("m30")
        self.assertEqual(template.findtext(".//wpml:templateType", namespaces=namespace), "waypoint")
        self.assertEqual(template.findtext(".//kml:Folder/wpml:globalHeight", namespaces=namespace), "80")
        self.assertEqual(waylines.findtext(".//wpml:executeHeightMode", namespaces=namespace), "relativeToStartPoint")
        indices = waylines.findall(".//wpml:index", namespace)
        self.assertEqual([item.text for item in indices], ["0", "1", "2"])
        self.assertEqual(waylines.findall(".//kml:coordinates", namespace)[0].text, "119.95,31.67")

    def test_builds_dji_fly_native_archive(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.951, 31.672]], 80, 36, "fly")
        with ZipFile(BytesIO(payload)) as archive:
            template = archive.read("wpmz/template.kml").decode("utf-8")
            waylines = archive.read("wpmz/waylines.wpml").decode("utf-8")
        self.assertIn('xmlns:wpml="http://www.uav.com/wpmz/1.0.2"', template)
        self.assertIn("<wpml:author>fly</wpml:author>", template)
        # Fly 原生格式：template 只有 missionConfig，几何全在 waylines。
        self.assertNotIn("<Folder>", template)
        self.assertNotIn("<Placemark>", template)
        self.assertIn("<wpml:droneEnumValue>68</wpml:droneEnumValue>", waylines)
        self.assertIn("<wpml:waypointHeadingPathMode>followBadArc</wpml:waypointHeadingPathMode>", waylines)
        self.assertIn("<wpml:waypointTurnMode>toPointAndStopWithContinuityCurvature</wpml:waypointTurnMode>", waylines)
        self.assertIn("<wpml:waypointGimbalPitchAngle>-90</wpml:waypointGimbalPitchAngle>", waylines)
        self.assertNotIn("payloadInfo", template)
        self.assertNotIn("takeOffSecurityHeight", waylines)
        self.assertNotIn("globalRTHHeight", waylines)

    def test_writes_per_waypoint_settings_for_enterprise_profiles(self):
        payload = build_dji_kmz(
            [[119.95, 31.67], [119.951, 31.672], [119.952, 31.672]],
            80,
            36,
            "m30",
            [
                {"altitude_m": 60, "heading_mode": "fixed", "heading_angle": 90, "gimbal_pitch_angle": -30},
                {"altitude_m": 80, "heading_mode": "smoothTransition", "heading_angle": 180, "gimbal_pitch_angle": -45},
                {"altitude_m": 100, "heading_mode": "followWayline", "heading_angle": 0, "gimbal_pitch_angle": 0},
            ],
        )
        with ZipFile(BytesIO(payload)) as archive:
            namespace = namespace_for("m30")
            template = ElementTree.fromstring(archive.read("wpmz/template.kml"))
            waylines = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
        self.assertEqual(template.findtext(".//wpml:gimbalPitchMode", namespaces=namespace), "usePointSetting")
        for document in (template, waylines):
            placemarks = document.findall(".//kml:Placemark", namespace)
            self.assertEqual(len(placemarks), 3)
            self.assertEqual(placemarks[0].findtext("wpml:waypointHeadingParam/wpml:waypointHeadingMode", namespaces=namespace), "smoothTransition")
            self.assertEqual(placemarks[0].findtext("wpml:waypointHeadingParam/wpml:waypointHeadingAngle", namespaces=namespace), "90")
            self.assertEqual(placemarks[0].findtext("wpml:gimbalPitchAngle", namespaces=namespace), "-30")
            self.assertEqual(placemarks[1].findtext("wpml:gimbalPitchAngle", namespaces=namespace), "-45")
        self.assertEqual(waylines.findtext(".//wpml:executeHeight", namespaces=namespace), "60")

    def test_writes_per_waypoint_settings_in_fly_native_format(self):
        payload = build_dji_kmz(
            [[119.95, 31.67], [119.951, 31.672], [119.952, 31.672]],
            80,
            36,
            "fly",
            [
                {"altitude_m": 60, "heading_mode": "fixed", "heading_angle": 90, "gimbal_pitch_angle": -30},
                {"altitude_m": 80, "heading_mode": "smoothTransition", "heading_angle": 180, "gimbal_pitch_angle": -45},
                {},
            ],
        )
        with ZipFile(BytesIO(payload)) as archive:
            waylines = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
        namespace = namespace_for("fly")
        placemarks = waylines.findall(".//kml:Placemark", namespace)
        self.assertEqual(len(placemarks), 3)
        self.assertEqual([point.findtext("wpml:waypointGimbalHeadingParam/wpml:waypointGimbalPitchAngle", namespaces=namespace) for point in placemarks], ["-30", "-45", "-90"])
        self.assertEqual(placemarks[0].findtext("wpml:waypointHeadingParam/wpml:waypointHeadingMode", namespaces=namespace), "smoothTransition")
        self.assertEqual(placemarks[0].findtext("wpml:executeHeight", namespaces=namespace), "60")
        self.assertEqual(placemarks[0].findtext("wpml:waypointHeadingParam/wpml:waypointHeadingAngleEnable", namespaces=namespace), "1")

    def test_preserves_edited_orientation_without_exporting_every_default_point(self):
        coordinates = [[119.95 + index * 0.0001, 31.67] for index in range(10)]
        settings = [
            {"altitude_m": 80, "heading_mode": "followWayline", "heading_angle": 0, "gimbal_pitch_angle": -90}
            for _ in coordinates
        ]
        settings[4].update({"heading_mode": "smoothTransition", "heading_angle": 315, "gimbal_pitch_angle": -48, "export_anchor": True})
        payload = build_dji_kmz(coordinates, 80, 36, "fly", settings)
        with ZipFile(BytesIO(payload)) as archive:
            namespace = namespace_for("fly")
            document = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
        placemarks = document.findall(".//kml:Placemark", namespace)
        self.assertLess(len(placemarks), len(coordinates))
        edited = [point for point in placemarks if point.findtext("kml:Point/kml:coordinates", namespaces=namespace) == "119.9504,31.67"]
        self.assertEqual(len(edited), 1)
        self.assertEqual(edited[0].findtext("wpml:waypointHeadingParam/wpml:waypointHeadingMode", namespaces=namespace), "smoothTransition")
        self.assertEqual(edited[0].findtext("wpml:waypointHeadingParam/wpml:waypointHeadingAngle", namespaces=namespace), "-45")
        self.assertEqual(edited[0].findtext("wpml:waypointGimbalHeadingParam/wpml:waypointGimbalPitchAngle", namespaces=namespace), "-48")

    def test_simplifies_long_uniform_camera_range_without_losing_boundaries(self):
        coordinates = []
        for index in range(640):
            rise = min(max(index - 100, 0), max(540 - index, 0)) / 220
            coordinates.append([119.95 + index * 0.00005, 31.67 + rise * 0.0005])
        settings = [{} for _ in coordinates]
        for index in range(100, 541):
            settings[index] = {"heading_mode": "fixed", "heading_angle": 72, "gimbal_pitch_angle": -35}
        settings[300]["export_anchor"] = True

        payload = build_dji_kmz(coordinates, 80, 36, "fly", settings)
        namespace = namespace_for("fly")
        with ZipFile(BytesIO(payload)) as archive:
            points = ElementTree.fromstring(archive.read("wpmz/waylines.wpml")).findall(".//kml:Placemark", namespace)
            self.assertLess(len(points), 500)
            by_source_index = {
                round((float(point.findtext("kml:Point/kml:coordinates", namespaces=namespace).split(",")[0]) - 119.95) / 0.00005): point
                for point in points
            }
            for index in (99, 100, 300, 320, 540, 541):
                self.assertIn(index, by_source_index)
            for index, point in by_source_index.items():
                heading = point.find("wpml:waypointHeadingParam", namespace)
                if 100 <= index <= 540:
                    self.assertEqual(heading.findtext("wpml:waypointHeadingMode", namespaces=namespace), "smoothTransition")
                    self.assertEqual(heading.findtext("wpml:waypointHeadingAngle", namespaces=namespace), "72")
                    self.assertEqual(point.findtext("wpml:waypointGimbalHeadingParam/wpml:waypointGimbalPitchAngle", namespaces=namespace), "-35")
                else:
                    self.assertEqual(heading.findtext("wpml:waypointHeadingMode", namespaces=namespace), "followWayline")
                    self.assertEqual(point.findtext("wpml:waypointGimbalHeadingParam/wpml:waypointGimbalPitchAngle", namespaces=namespace), "-90")

    def test_rejects_too_many_distinct_camera_settings(self):
        coordinates = [[119.95 + index * 0.00005, 31.67] for index in range(502)]
        settings = [{"gimbal_pitch_angle": -30 + index * 0.001} for index in range(502)]
        with self.assertRaisesRegex(KmzExportError, "已编辑航点超过 500 个"):
            build_dji_kmz(coordinates, 80, 36, "fly", settings)

    def test_unedited_waypoint_settings_still_allow_simplification(self):
        coordinates = [[119.95 + index * 0.0001, 31.67] for index in range(10)]
        payload = build_dji_kmz(coordinates, 80, 36, "fly", [{} for _ in coordinates])
        with ZipFile(BytesIO(payload)) as archive:
            waylines = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
        namespace = namespace_for("fly")
        placemarks = waylines.findall(".//kml:Placemark", namespace)
        self.assertLess(len(placemarks), len(coordinates))
        self.assertTrue(all(point.findtext("wpml:waypointGimbalHeadingParam/wpml:waypointGimbalPitchAngle", namespaces=namespace) == "-90" for point in placemarks))

    def test_pitch_only_second_waypoint_keeps_course_heading(self):
        coordinates = [[119.952, 31.67], [119.951, 31.67], [119.95, 31.67]]
        settings = [{} for _ in coordinates]
        settings[1].update({"gimbal_pitch_angle": -53.8, "export_anchor": True})
        for profile in ("fly", "m30", "m3e"):
            with self.subTest(profile=profile):
                payload = build_dji_kmz(coordinates, 100, 40, profile, settings)
                namespace = namespace_for(profile)
                pitch_tag = ("wpml:waypointGimbalHeadingParam/wpml:waypointGimbalPitchAngle" if profile == "fly" else "wpml:gimbalPitchAngle")
                with ZipFile(BytesIO(payload)) as archive:
                    points = ElementTree.fromstring(archive.read("wpmz/waylines.wpml")).findall(".//kml:Placemark", namespace)
                    self.assertEqual(len(points), 3)
                    self.assertEqual([point.findtext(pitch_tag, namespaces=namespace) for point in points], ["-90", "-53.8", "-90"])
                    for point in points:
                        heading = point.find("wpml:waypointHeadingParam", namespace)
                        self.assertEqual(heading.findtext("wpml:waypointHeadingMode", namespaces=namespace), "followWayline")
                        self.assertEqual(heading.findtext("wpml:waypointHeadingAngle", namespaces=namespace), "-90")
                        self.assertEqual(heading.findtext("wpml:waypointHeadingPathMode", namespaces=namespace), "followBadArc")

    def test_custom_heading_across_north_keeps_shortest_rotation_setting(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.951, 31.67]], 100, 40, "fly", [
            {"heading_mode": "smoothTransition", "heading_angle": 359},
            {"heading_mode": "smoothTransition", "heading_angle": 1},
        ])
        namespace = namespace_for("fly")
        with ZipFile(BytesIO(payload)) as archive:
            headings = ElementTree.fromstring(archive.read("wpmz/waylines.wpml")).findall(".//kml:Placemark/wpml:waypointHeadingParam", namespace)
            self.assertEqual([heading.findtext("wpml:waypointHeadingAngle", namespaces=namespace) for heading in headings], ["-1", "1"])
            self.assertTrue(all(heading.findtext("wpml:waypointHeadingPathMode", namespaces=namespace) == "followBadArc" for heading in headings))

    def test_rejects_unexecutable_manual_heading(self):
        points = [[119.95, 31.67], [119.951, 31.67]]
        with self.assertRaisesRegex(KmzExportError, "手动控制"):
            build_dji_kmz(points, 80, 36, "fly", [{}, {"heading_mode": "manually"}])

    def test_merges_duplicate_edited_waypoints_with_latter_setting(self):
        points = [[119.95, 31.67], [119.9500005, 31.67], [119.951, 31.67]]
        settings = [
            {"altitude_m": 60, "heading_mode": "fixed", "heading_angle": 45, "gimbal_pitch_angle": -30, "export_anchor": True},
            {"altitude_m": 100, "heading_mode": "fixed", "heading_angle": 90, "gimbal_pitch_angle": -60, "export_anchor": True},
            {},
        ]
        merge_report: list[tuple[int, int]] = []
        payload = build_dji_kmz(points, 80, 36, "fly", settings, merge_report=merge_report)
        self.assertEqual(merge_report, [(0, 1)])
        namespace = namespace_for("fly")
        with ZipFile(BytesIO(payload)) as archive:
            document = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
            placemarks = document.findall(".//kml:Placemark", namespace)
            self.assertEqual(len(placemarks), 2)
            merged = placemarks[0]
            self.assertEqual(merged.findtext("kml:Point/kml:coordinates", namespaces=namespace), "119.9500005,31.67")
            self.assertEqual(merged.findtext("wpml:waypointHeadingParam/wpml:waypointHeadingAngle", namespaces=namespace), "90")
            self.assertEqual(merged.findtext("wpml:waypointGimbalHeadingParam/wpml:waypointGimbalPitchAngle", namespaces=namespace), "-60")
            self.assertEqual(merged.findtext("wpml:executeHeight", namespaces=namespace), "100")

    def test_merges_coincident_waypoint_silently_when_front_is_unedited(self):
        points = [[119.95, 31.67], [119.95, 31.67], [119.951, 31.67]]
        merge_report: list[tuple[int, int]] = []
        payload = build_dji_kmz(points, 80, 36, "fly", [{}, {"export_anchor": True}, {}], merge_report=merge_report)
        self.assertEqual(merge_report, [])
        namespace = namespace_for("fly")
        with ZipFile(BytesIO(payload)) as archive:
            waylines = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
        placemarks = waylines.findall(".//kml:Placemark", namespace)
        self.assertEqual(
            [point.findtext("kml:Point/kml:coordinates", namespaces=namespace) for point in placemarks],
            ["119.95,31.67", "119.951,31.67"],
        )

    def test_rejects_more_edited_waypoints_than_export_limit(self):
        coordinates = [(119.95 + index * 0.0001, 31.67) for index in range(502)]
        with self.assertRaisesRegex(KmzExportError, "已编辑航点超过"):
            _simplify_coordinate_indices(coordinates, set(range(502)))

    def test_custom_waypoint_is_kept_when_geometry_is_straight(self):
        payload = build_dji_kmz(
            [[119.95, 31.67], [119.951, 31.67], [119.952, 31.67]],
            80,
            36,
            "fly",
            [{}, {"altitude_m": 120, "heading_mode": "fixed", "heading_angle": 45}, {}],
        )
        with ZipFile(BytesIO(payload)) as archive:
            waylines = archive.read("wpmz/waylines.wpml").decode("utf-8")
        self.assertEqual(waylines.count("<wpml:index>"), 3)
        self.assertIn("<wpml:executeHeight>120</wpml:executeHeight>", waylines)

    def test_rejects_speed_above_dji_waypoint_limit(self):
        with self.assertRaises(KmzExportError):
            build_dji_kmz([[0, 0], [1, 1]], 80, 54.1)
        with self.assertRaises(KmzExportError):
            build_dji_kmz([[0, 0], [1, 1]], 80, 3.5)

    def test_rejects_fly_speed_above_device_capability(self):
        with self.assertRaisesRegex(KmzExportError, "43.2"):
            build_dji_kmz([[119.95, 31.67], [119.951, 31.672]], 80, 45, "fly")
        # 行业档位不受消费端能力限制（Pilot 2 机型上限更高）。
        build_dji_kmz([[119.95, 31.67], [119.951, 31.672]], 80, 45, "m30")

    def test_turn_damping_dist_is_positive_and_never_exceeds_segment(self):
        import math

        coordinates = [[119.95, 31.67], [119.9502, 31.67], [119.9509, 31.67], [119.9511, 31.6702]]
        for profile in ("fly", "m30"):
            with self.subTest(profile=profile):
                payload = build_dji_kmz(coordinates, 80, 36, profile)
                namespace = namespace_for(profile)
                with ZipFile(BytesIO(payload)) as archive:
                    placemarks = ElementTree.fromstring(archive.read("wpmz/waylines.wpml")).findall(".//kml:Placemark", namespace)
                    dampings = []
                    positions = []
                    for point in placemarks:
                        dampings.append(float(point.findtext("wpml:waypointTurnParam/wpml:waypointTurnDampingDist", namespaces=namespace)))
                        lon, lat = (float(value) for value in point.findtext("kml:Point/kml:coordinates", namespaces=namespace).split(",")[:2])
                        positions.append((lon, lat))
                    self.assertTrue(all(damping > 0 for damping in dampings))
                    for index in range(len(positions) - 1):
                        lon1, lat1 = positions[index]
                        lon2, lat2 = positions[index + 1]
                        segment = math.hypot((lon2 - lon1) * 111320 * math.cos(math.radians((lat1 + lat2) / 2)), (lat2 - lat1) * 111320)
                        self.assertLess(dampings[index] + dampings[index + 1], segment)

    def test_auto_record_writes_start_and_stop_action_groups(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.951, 31.672], [119.952, 31.672]], 80, 36, "fly", auto_record=True)
        namespace = namespace_for("fly")
        with ZipFile(BytesIO(payload)) as archive:
            template = ElementTree.fromstring(archive.read("wpmz/template.kml"))
            document = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
            groups = document.findall(".//wpml:actionGroup", namespace)
            self.assertEqual(len(groups), 2)
            first, last = groups
            self.assertEqual(first.findtext("wpml:actionGroupId", namespaces=namespace), "0")
            self.assertEqual(first.findtext("wpml:actionGroupStartIndex", namespaces=namespace), "0")
            self.assertEqual(first.findtext("wpml:actionGroupEndIndex", namespaces=namespace), "0")
            self.assertEqual(first.findtext("wpml:actionGroupMode", namespaces=namespace), "parallel")
            self.assertEqual(first.findtext("wpml:actionTrigger/wpml:actionTriggerType", namespaces=namespace), "reachPoint")
            self.assertEqual(first.findtext("wpml:action/wpml:actionActuatorFunc", namespaces=namespace), "startRecord")
            self.assertEqual(first.findtext("wpml:action/wpml:actionActuatorFuncParam/wpml:payloadPositionIndex", namespaces=namespace), "0")
            self.assertEqual(first.findtext("wpml:action/wpml:actionActuatorFuncParam/wpml:useGlobalPayloadLensIndex", namespaces=namespace), "0")
            self.assertEqual(last.findtext("wpml:actionGroupId", namespaces=namespace), "1")
            self.assertEqual(last.findtext("wpml:actionGroupStartIndex", namespaces=namespace), "2")
            self.assertEqual(last.findtext("wpml:action/wpml:actionActuatorFunc", namespaces=namespace), "stopRecord")
            placemarks = document.findall(".//kml:Placemark", namespace)
            self.assertIsNotNone(placemarks[0].find("wpml:actionGroup", namespace))
            self.assertIsNotNone(placemarks[-1].find("wpml:actionGroup", namespace))
            self.assertIsNone(placemarks[1].find("wpml:actionGroup", namespace))
            # Fly 原生 template 无 Folder，自然不含动作组。
            self.assertEqual(template.findall(".//wpml:actionGroup", namespace), [])

    def test_auto_record_disabled_by_default(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.951, 31.672]], 80, 36, "fly")
        namespace = namespace_for("fly")
        with ZipFile(BytesIO(payload)) as archive:
            for name in ("wpmz/template.kml", "wpmz/waylines.wpml"):
                with self.subTest(name=name):
                    self.assertEqual(ElementTree.fromstring(archive.read(name)).findall(".//wpml:actionGroup", namespace), [])

    def test_default_gimbal_pitch_looks_straight_down(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.951, 31.672], [119.952, 31.672]], 80, 36, "fly")
        with ZipFile(BytesIO(payload)) as archive:
            waylines = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
        namespace = namespace_for("fly")
        pitches = [point.findtext("wpml:waypointGimbalHeadingParam/wpml:waypointGimbalPitchAngle", namespaces=namespace) for point in waylines.findall(".//kml:Placemark", namespace)]
        self.assertEqual(pitches, ["-90", "-90", "-90"])

    def test_explicit_horizontal_pitch_is_preserved(self):
        payload = build_dji_kmz(
            [[119.95, 31.67], [119.951, 31.672], [119.952, 31.672]],
            80,
            36,
            "fly",
            [{"gimbal_pitch_angle": 0}, {}, {"gimbal_pitch_angle": -30}],
        )
        namespace = namespace_for("fly")
        with ZipFile(BytesIO(payload)) as archive:
            waylines = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
        pitches = [point.findtext("wpml:waypointGimbalHeadingParam/wpml:waypointGimbalPitchAngle", namespaces=namespace) for point in waylines.findall(".//kml:Placemark", namespace)]
        self.assertEqual(pitches, ["0", "-90", "-30"])


if __name__ == "__main__":
    unittest.main()
