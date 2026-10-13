import unittest
from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree

from dji_kmz import KmzExportError, _simplify_coordinate_indices, _simplify_coordinates, build_dji_kmz


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
        namespace = {"wpml": "http://www.dji.com/wpmz/1.0.2", "kml": "http://www.opengis.net/kml/2.2"}
        self.assertEqual(template.findtext(".//wpml:templateType", namespaces=namespace), "waypoint")
        self.assertEqual(template.findtext(".//kml:Folder/wpml:globalHeight", namespaces=namespace), "80")
        self.assertEqual(waylines.findtext(".//wpml:executeHeightMode", namespaces=namespace), "relativeToStartPoint")
        indices = waylines.findall(".//wpml:index", namespace)
        self.assertEqual([item.text for item in indices], ["0", "1", "2"])
        self.assertEqual(waylines.findall(".//kml:coordinates", namespace)[0].text, "119.95,31.67")

    def test_builds_dji_fly_profile_without_enterprise_payload(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.951, 31.672], [119.952, 31.672]], 80, 36, "fly")
        with ZipFile(BytesIO(payload)) as archive:
            template = archive.read("wpmz/template.kml").decode("utf-8")
            waylines = archive.read("wpmz/waylines.wpml").decode("utf-8")
        self.assertIn('xmlns:wpml="http://www.uav.com/wpmz/1.0.2"', template)
        self.assertIn("<wpml:author>fly</wpml:author>", template)
        self.assertIn("<wpml:droneEnumValue>68</wpml:droneEnumValue>", waylines)
        self.assertIn("<wpml:waypointHeadingPathMode>followBadArc</wpml:waypointHeadingPathMode>", waylines)
        self.assertNotIn("payloadInfo", template)

    def test_writes_per_waypoint_height_heading_and_gimbal_pitch(self):
        payload = build_dji_kmz(
            [[119.95, 31.67], [119.951, 31.672], [119.952, 31.672]],
            80,
            36,
            "fly",
            [
                {"altitude_m": 60, "heading_mode": "fixed", "heading_angle": 90, "gimbal_pitch_angle": -30},
                {"altitude_m": 80, "heading_mode": "smoothTransition", "heading_angle": 180, "gimbal_pitch_angle": -45},
                {"altitude_m": 100, "heading_mode": "followWayline", "heading_angle": 0, "gimbal_pitch_angle": 0},
            ],
        )
        with ZipFile(BytesIO(payload)) as archive:
            namespace = {"wpml": "http://www.uav.com/wpmz/1.0.2", "kml": "http://www.opengis.net/kml/2.2"}
            template = ElementTree.fromstring(archive.read("wpmz/template.kml"))
            waylines = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
        self.assertEqual(template.findtext(".//wpml:gimbalPitchMode", namespaces=namespace), "usePointSetting")
        for document in (template, waylines):
            placemarks = document.findall(".//kml:Placemark", namespace)
            self.assertEqual(len(placemarks), 3)
            self.assertEqual(placemarks[0].findtext("wpml:waypointHeadingParam/wpml:waypointHeadingMode", namespaces=namespace), "smoothTransition")
            self.assertEqual(placemarks[0].findtext("wpml:waypointHeadingParam/wpml:waypointHeadingAngle", namespaces=namespace), "90")
            self.assertEqual(placemarks[0].findtext("wpml:gimbalPitchAngle", namespaces=namespace), "-30")
            self.assertEqual(placemarks[1].findtext("wpml:waypointHeadingParam/wpml:waypointHeadingMode", namespaces=namespace), "smoothTransition")
            self.assertEqual(placemarks[1].findtext("wpml:gimbalPitchAngle", namespaces=namespace), "-45")
        self.assertEqual(waylines.findtext(".//wpml:executeHeight", namespaces=namespace), "60")

    def test_preserves_edited_orientation_without_exporting_every_default_point(self):
        coordinates = [[119.95 + index * 0.0001, 31.67] for index in range(10)]
        settings = [
            {"altitude_m": 80, "heading_mode": "followWayline", "heading_angle": 0, "gimbal_pitch_angle": -30}
            for _ in coordinates
        ]
        settings[4].update({"heading_mode": "smoothTransition", "heading_angle": 315, "gimbal_pitch_angle": -48, "export_anchor": True})
        payload = build_dji_kmz(coordinates, 80, 36, "fly", settings)
        with ZipFile(BytesIO(payload)) as archive:
            namespace = {"wpml": "http://www.uav.com/wpmz/1.0.2", "kml": "http://www.opengis.net/kml/2.2"}
            documents = [ElementTree.fromstring(archive.read(name)) for name in ("wpmz/template.kml", "wpmz/waylines.wpml")]
        for document in documents:
            placemarks = document.findall(".//kml:Placemark", namespace)
            self.assertLess(len(placemarks), len(coordinates))
            edited = [point for point in placemarks if point.findtext("kml:Point/kml:coordinates", namespaces=namespace) == "119.9504,31.67"]
            self.assertEqual(len(edited), 1)
            self.assertEqual(edited[0].findtext("wpml:waypointHeadingParam/wpml:waypointHeadingMode", namespaces=namespace), "smoothTransition")
            self.assertEqual(edited[0].findtext("wpml:waypointHeadingParam/wpml:waypointHeadingAngle", namespaces=namespace), "-45")
            self.assertEqual(edited[0].findtext("wpml:gimbalPitchAngle", namespaces=namespace), "-48")

    def test_unedited_waypoint_settings_still_allow_simplification(self):
        coordinates = [[119.95 + index * 0.0001, 31.67] for index in range(10)]
        payload = build_dji_kmz(coordinates, 80, 36, "fly", [{} for _ in coordinates])
        with ZipFile(BytesIO(payload)) as archive:
            waylines = ElementTree.fromstring(archive.read("wpmz/waylines.wpml"))
        namespace = {"wpml": "http://www.uav.com/wpmz/1.0.2", "kml": "http://www.opengis.net/kml/2.2"}
        placemarks = waylines.findall(".//kml:Placemark", namespace)
        self.assertLess(len(placemarks), len(coordinates))
        self.assertTrue(all(point.findtext("wpml:gimbalPitchAngle", namespaces=namespace) == "-30" for point in placemarks))

    def test_pitch_only_second_waypoint_keeps_course_heading_in_both_files(self):
        coordinates = [[119.952, 31.67], [119.951, 31.67], [119.95, 31.67]]
        settings = [{"heading_mode": "followWayline", "heading_angle": 0, "gimbal_pitch_angle": -30} for _ in coordinates]
        settings[1].update({"gimbal_pitch_angle": -53.8, "export_anchor": True})
        for profile in ("fly", "m30", "m3e"):
            with self.subTest(profile=profile):
                payload = build_dji_kmz(coordinates, 100, 45, profile, settings)
                with ZipFile(BytesIO(payload)) as archive:
                    wpml_namespace = "http://www.uav.com/wpmz/1.0.2" if profile == "fly" else "http://www.dji.com/wpmz/1.0.2"
                    namespace = {"wpml": wpml_namespace, "kml": "http://www.opengis.net/kml/2.2"}
                    for name in ("wpmz/template.kml", "wpmz/waylines.wpml"):
                        points = ElementTree.fromstring(archive.read(name)).findall(".//kml:Placemark", namespace)
                        self.assertEqual(len(points), 3)
                        self.assertEqual([point.findtext("wpml:gimbalPitchAngle", namespaces=namespace) for point in points], ["-30", "-53.8", "-30"])
                        for point in points:
                            heading = point.find("wpml:waypointHeadingParam", namespace)
                            self.assertEqual(heading.findtext("wpml:waypointHeadingMode", namespaces=namespace), "followWayline")
                            self.assertEqual(heading.findtext("wpml:waypointHeadingAngle", namespaces=namespace), "-90")
                            self.assertEqual(heading.findtext("wpml:waypointHeadingPathMode", namespaces=namespace), "followBadArc")

    def test_custom_heading_across_north_keeps_shortest_rotation_setting(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.951, 31.67]], 100, 45, "fly", [
            {"heading_mode": "smoothTransition", "heading_angle": 359},
            {"heading_mode": "smoothTransition", "heading_angle": 1},
        ])
        namespace = {"wpml": "http://www.uav.com/wpmz/1.0.2", "kml": "http://www.opengis.net/kml/2.2"}
        with ZipFile(BytesIO(payload)) as archive:
            for name in ("wpmz/template.kml", "wpmz/waylines.wpml"):
                headings = ElementTree.fromstring(archive.read(name)).findall(".//kml:Placemark/wpml:waypointHeadingParam", namespace)
                self.assertEqual([heading.findtext("wpml:waypointHeadingAngle", namespaces=namespace) for heading in headings], ["-1", "1"])
                self.assertTrue(all(heading.findtext("wpml:waypointHeadingPathMode", namespaces=namespace) == "followBadArc" for heading in headings))

    def test_rejects_unexecutable_manual_heading_and_duplicate_edited_point(self):
        points = [[119.95, 31.67], [119.951, 31.67]]
        with self.assertRaisesRegex(KmzExportError, "手动控制"):
            build_dji_kmz(points, 80, 36, "fly", [{}, {"heading_mode": "manually"}])
        with self.assertRaisesRegex(KmzExportError, "位置重合"):
            build_dji_kmz([points[0], points[0], points[1]], 80, 36, "fly", [{}, {"export_anchor": True}, {}])

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


if __name__ == "__main__":
    unittest.main()
