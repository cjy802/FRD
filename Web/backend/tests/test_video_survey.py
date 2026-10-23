"""Unit tests for SRT recording-clock parsing and task route matching."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

from starlette.datastructures import UploadFile

from video_survey import (VideoSurveyStore, analyse_route_match, parse_srt_recording_window,
                          parse_srt_telemetry)

LEGACY_SRT = """1
00:00:00,000 --> 00:00:00,033
FrameCnt: 1
2026-10-23. 10:30:45,000
[latitude: 30.0] [longitude: 120.0] [rel_alt: 50.0 abs_alt: 100.0]

2
00:00:01,000 --> 00:00:01,033
FrameCnt: 31
2026-10-23. 10:30:46,000
[latitude: 30.0] [longitude: 120.002] [rel_alt: 50.0 abs_alt: 100.0]

3
00:00:02,000 --> 00:00:02,033
FrameCnt: 61
2026-10-23. 10:30:47,000
[latitude: 30.0] [longitude: 120.004] [rel_alt: 50.0 abs_alt: 100.0]

4
00:00:03,000 --> 00:00:03,033
FrameCnt: 91
2026-10-23. 10:30:48,000
[latitude: 30.0] [longitude: 120.006] [rel_alt: 50.0 abs_alt: 100.0]

5
00:00:04,000 --> 00:00:04,033
FrameCnt: 121
2026-10-23. 10:30:49,000
[latitude: 30.0] [longitude: 120.008] [rel_alt: 50.0 abs_alt: 100.0]

6
00:00:05,000 --> 00:00:05,033
FrameCnt: 151
2026-10-23. 10:30:50,000
[latitude: 30.0] [longitude: 120.01] [rel_alt: 50.0 abs_alt: 100.0]
"""

NEW_FLY_SRT = """1
00:00:00,000 --> 00:00:00,016
HOME(120.0000000,30.0000000,100) 2026-10-23 10:30:45.000 [shot_number:1]
GPS(120.0000000,30.0000000,25) 2026-10-23 10:30:45.083
"""

NO_CLOCK_SRT = """1
00:00:00,000 --> 00:00:00,033
[latitude: 30.0] [longitude: 120.0] [rel_alt: 50.0 abs_alt: 100.0]
"""


def _task_record(coordinates: list[list[float]] | None) -> dict:
    route = {"type": "Feature", "properties": {},
             "geometry": {"type": "LineString", "coordinates": coordinates or []}}
    return {"task_id": "t", "route": route}


def _track_along(longitude_start: float, latitude: float, count: int, offset_deg: float) -> list[dict]:
    return [
        {"time_seconds": index, "latitude": latitude + offset_deg,
         "longitude": longitude_start + index * 0.002, "altitude_m": 50, "heading_deg": 90,
         "gimbal_pitch_deg": 0}
        for index in range(count)
    ]


class ParseSrtRecordingWindowTests(unittest.TestCase):
    def test_legacy_dji_format(self) -> None:
        window = parse_srt_recording_window(LEGACY_SRT)
        self.assertEqual(window["started_at_local"], "2026-10-23 10:30:45.000")
        self.assertEqual(window["ended_at_local"], "2026-10-23 10:30:50.000")

    def test_new_fly_format(self) -> None:
        window = parse_srt_recording_window(NEW_FLY_SRT)
        self.assertIsNotNone(window)
        self.assertEqual(window["started_at_local"], "2026-10-23 10:30:45.000")

    def test_missing_clock_returns_none(self) -> None:
        self.assertIsNone(parse_srt_recording_window(NO_CLOCK_SRT))


class AnalyseRouteMatchTests(unittest.TestCase):
    # 0.002 deg longitude ~ 192 m steps; the route spans the same segment.
    ROUTE = [[120.0, 30.0], [120.01, 30.0], [120.02, 30.0], [120.03, 30.0]]

    def test_matched_track(self) -> None:
        points = _track_along(120.0, 30.0, 20, offset_deg=4.5e-5)  # ~5 m north of the line
        result = analyse_route_match(points, _task_record(self.ROUTE))
        self.assertEqual(result["status"], "matched")
        self.assertLess(result["median_deviation_m"], 10)
        self.assertGreaterEqual(result["within_30m_percent"], 60)

    def test_near_track(self) -> None:
        points = _track_along(120.0, 30.0, 20, offset_deg=3.6e-4)  # ~40 m north
        result = analyse_route_match(points, _task_record(self.ROUTE))
        self.assertEqual(result["status"], "near")

    def test_off_route_track(self) -> None:
        points = _track_along(120.0, 30.05, 20, offset_deg=0)  # ~5.5 km away
        result = analyse_route_match(points, _task_record(self.ROUTE))
        self.assertEqual(result["status"], "off_route")
        self.assertGreater(result["median_deviation_m"], 1000)

    def test_too_few_points(self) -> None:
        result = analyse_route_match(_track_along(120.0, 30.0, 3, 0), _task_record(self.ROUTE))
        self.assertEqual(result["status"], "no_gps")

    def test_missing_route_geometry(self) -> None:
        result = analyse_route_match(_track_along(120.0, 30.0, 20, 0), _task_record(None))
        self.assertEqual(result["status"], "no_route")

    def test_without_task_record(self) -> None:
        self.assertIsNone(analyse_route_match(_track_along(120.0, 30.0, 20, 0), None))


class VideoSurveyCreateTests(unittest.TestCase):
    def test_create_writes_recording_and_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            store = VideoSurveyStore(storage_dir=storage)
            store.task_storage_dir = storage / "tasks"
            video = UploadFile(file=io.BytesIO(b"fake-video"), filename="DJI_0001.MP4")
            srt = UploadFile(file=io.BytesIO(LEGACY_SRT.encode("utf-8")), filename="DJI_0001.SRT")
            task_record = _task_record([[120.0, 30.0], [120.01, 30.0]])

            result = asyncio.run(store.create(video, srt, "20261023-abcd1234", task_record))

            self.assertEqual(result["telemetry_points"], 6)
            self.assertEqual(result["recording"]["started_at_local"], "2026-10-23 10:30:45.000")
            self.assertEqual(result["task_match"]["status"], "matched")
            directory = store._directory(result["job_id"])
            metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["task_match"]["status"], "matched")
            self.assertIn("video-surveys", str(directory))

    def test_create_without_task_skips_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            store = VideoSurveyStore(storage_dir=storage)
            video = UploadFile(file=io.BytesIO(b"fake-video"), filename="clip.mp4")
            srt = UploadFile(file=io.BytesIO(NO_CLOCK_SRT.encode("utf-8")), filename="clip.srt")

            result = asyncio.run(store.create(video, srt, None, None))

            self.assertTrue(result["has_gps"])
            self.assertIsNone(result["recording"])
            self.assertIsNone(result["task_match"])


if __name__ == "__main__":
    unittest.main()
