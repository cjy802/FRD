import unittest
from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree

from dji_kmz import KmzExportError, build_dji_kmz


class DjiKmzTests(unittest.TestCase):
    def test_builds_wpml_archive_with_matching_waypoints(self):
        payload = build_dji_kmz([[119.95, 31.67], [119.951, 31.671], [119.952, 31.672]], 80, 36)
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

    def test_rejects_speed_above_dji_waypoint_limit(self):
        with self.assertRaises(KmzExportError):
            build_dji_kmz([[0, 0], [1, 1]], 80, 54.1)
        with self.assertRaises(KmzExportError):
            build_dji_kmz([[0, 0], [1, 1]], 80, 3.5)


if __name__ == "__main__":
    unittest.main()
