"""Select video frames from measured image novelty and SRT path turns."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import time
from pathlib import Path

import cv2
import numpy as np


def _interpolate(points: list[dict], times: list[float], seconds: float) -> dict | None:
    if not points:
        return None
    index = bisect.bisect_left(times, seconds)
    if index <= 0:
        return points[0] if abs(times[0] - seconds) <= 2 else None
    if index >= len(points):
        return points[-1] if abs(seconds - times[-1]) <= 2 else None
    before, after = points[index - 1], points[index]
    gap = times[index] - times[index - 1]
    if gap <= 0:
        return before
    ratio = min(max((seconds - times[index - 1]) / gap, 0), 1)
    result = {"time_seconds": seconds}
    for key in ("latitude", "longitude", "altitude_m", "gimbal_pitch_deg"):
        first, second = before.get(key), after.get(key)
        result[key] = first + (second - first) * ratio if first is not None and second is not None else first if first is not None else second
    return result


def _distance_m(first: dict | None, second: dict | None) -> float | None:
    if not first or not second or first.get("latitude") is None or second.get("latitude") is None:
        return None
    lat1, lon1 = math.radians(first["latitude"]), math.radians(first["longitude"])
    lat2, lon2 = math.radians(second["latitude"]), math.radians(second["longitude"])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_008.8 * 2 * math.asin(min(1, math.sqrt(hav)))


def _bearing_deg(first: dict | None, second: dict | None) -> float | None:
    distance = _distance_m(first, second)
    if distance is None or distance < 1.0:
        return None
    lat1, lat2 = math.radians(first["latitude"]), math.radians(second["latitude"])
    dlon = math.radians(second["longitude"] - first["longitude"])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _turn_change(points: list[dict], times: list[float], seconds: float, window: float = 1.5) -> float | None:
    before, middle, after = (_interpolate(points, times, seconds + offset) for offset in (-window, 0, window))
    first_bearing = _bearing_deg(before, middle)
    second_bearing = _bearing_deg(middle, after)
    if first_bearing is None or second_bearing is None:
        return None
    return abs((second_bearing - first_bearing + 180) % 360 - 180)


def _estimate_transform(source: tuple[np.ndarray, list], destination: tuple[np.ndarray, list]) -> tuple[np.ndarray | None, int, float]:
    source_descriptors, source_keypoints = source
    destination_descriptors, destination_keypoints = destination
    if source_descriptors is None or destination_descriptors is None or len(source_keypoints) < 12 or len(destination_keypoints) < 12:
        return None, 0, 0.0
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(source_descriptors, destination_descriptors, k=2)
    good = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance]
    if len(good) < 12:
        return None, len(good), 0.0
    source_points = np.float32([source_keypoints[match.queryIdx].pt for match in good])
    destination_points = np.float32([destination_keypoints[match.trainIdx].pt for match in good])
    transform, inliers = cv2.findHomography(source_points, destination_points, cv2.RANSAC, 3.5)
    if transform is None or inliers is None or not np.isfinite(transform).all():
        return None, len(good), 0.0
    inlier_count = int(inliers.sum())
    inlier_ratio = inlier_count / len(good)
    if inlier_count < 10 or inlier_ratio < 0.12:
        return None, inlier_count, inlier_ratio
    return transform, inlier_count, inlier_ratio


def _transformed_overlap_percent(transform: np.ndarray) -> float | None:
    height, width = 360, 640
    corners = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]).reshape(-1, 1, 2)
    warped = cv2.perspectiveTransform(corners, transform).reshape(-1, 2)
    if not np.isfinite(warped).all() or not cv2.isContourConvex(warped.astype(np.float32)):
        return None
    try:
        area, _ = cv2.intersectConvexConvex(warped.astype(np.float32),
                                            np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]))
    except cv2.error:
        return None
    return min(max(float(area) / (width * height), 0), 1) * 100


def _features(image: np.ndarray, orb: cv2.ORB) -> tuple[np.ndarray, list]:
    height, width = image.shape[:2]
    scale = min(640 / width, 360 / height)
    reduced = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
    if reduced.shape[1] != 640 or reduced.shape[0] != 360:
        reduced = cv2.resize(reduced, (640, 360), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(reduced, cv2.COLOR_BGR2GRAY)
    keypoints, descriptors = orb.detectAndCompute(gray, None)
    return descriptors, keypoints


def _save_frame(frame: np.ndarray, output_dir: Path, index: int, seconds: float) -> str:
    height, width = frame.shape[:2]
    scale = min(1.0, 1920 / max(width, height))
    if scale < 1:
        frame = cv2.resize(frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    name = f"frame_{index:06d}_{seconds:09.3f}s.jpg"
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise RuntimeError(f"无法保存选中的视频帧 {index}。")
    (output_dir / name).write_bytes(encoded.tobytes())
    return name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--telemetry", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-frames", required=True, type=int)
    parser.add_argument("--scan-fps", type=float, default=2.0)
    parser.add_argument("--straight-overlap", type=float, default=12.0)
    parser.add_argument("--turn-overlap", type=float, default=60.0)
    parser.add_argument("--turn-angle", type=float, default=18.0)
    parser.add_argument("--camera-change", type=float, default=12.0)
    args = parser.parse_args()
    if not args.video.is_file() or args.max_frames < 1 or args.scan_fps <= 0:
        print(json.dumps({"error": "视频、帧数上限或扫描频率无效。"}, ensure_ascii=False))
        return 2

    telemetry = json.loads(args.telemetry.read_text(encoding="utf-8"))
    telemetry = sorted((point for point in telemetry if point.get("latitude") is not None and point.get("longitude") is not None),
                       key=lambda point: point["time_seconds"])
    times = [point["time_seconds"] for point in telemetry]
    if len(telemetry) < 2:
        print(json.dumps({"error": "SRT 中可用的 GPS 记录不足，无法判断飞行方向和转弯。"}, ensure_ascii=False))
        return 2

    capture = cv2.VideoCapture(str(args.video))
    if not capture.isOpened():
        print(json.dumps({"error": "无法打开任务视频。"}, ensure_ascii=False))
        return 2
    fps = capture.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(fps) or fps <= 0:
        capture.release()
        print(json.dumps({"error": "无法读取视频帧率。"}, ensure_ascii=False))
        return 2
    expected_frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    stride = max(1, round(fps / args.scan_fps))
    orb = cv2.ORB_create(nfeatures=2200, scaleFactor=1.2, nlevels=6, fastThreshold=12)
    selected: list[dict] = []
    previous_features = None
    anchor_to_previous = np.eye(3, dtype=np.float64)
    anchor_telemetry = None
    scanned = 0
    frame_index = 0
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    while len(selected) < args.max_frames:
        ok, frame = capture.read()
        if not ok:
            break
        if frame_index % stride == 0:
            seconds = frame_index / fps
            position = _interpolate(telemetry, times, seconds)
            if position is not None:
                current_features = _features(frame, orb)
                scanned += 1
                if previous_features is None:
                    reason = "首帧建立覆盖基线"
                    overlap = None
                    turn_change = None
                    anchor_to_current = np.eye(3, dtype=np.float64)
                else:
                    step_transform, inlier_count, inlier_ratio = _estimate_transform(previous_features, current_features)
                    anchor_to_current = step_transform @ anchor_to_previous if step_transform is not None else None
                    overlap = _transformed_overlap_percent(anchor_to_current) if anchor_to_current is not None else None
                    turn_change = _turn_change(telemetry, times, seconds)
                    pitch_change = None
                    if anchor_telemetry.get("gimbal_pitch_deg") is not None and position.get("gimbal_pitch_deg") is not None:
                        pitch_change = abs(position["gimbal_pitch_deg"] - anchor_telemetry["gimbal_pitch_deg"])
                    turn_active = turn_change is not None and turn_change >= args.turn_angle
                    target_overlap = args.turn_overlap if turn_active else args.straight_overlap
                    if pitch_change is not None and pitch_change >= args.camera_change:
                        reason = "镜头角度明显变化"
                    elif turn_active and (overlap is None or overlap <= target_overlap):
                        reason = "转弯补覆盖"
                    elif overlap is None:
                        reason = "画面匹配不确定，保留以免漏区"
                    elif overlap <= target_overlap:
                        reason = "直线新增覆盖"
                    else:
                        reason = ""
                if reason:
                    name = _save_frame(frame, args.output_dir, frame_index, seconds)
                    selected.append({
                        "file": name,
                        "source_frame_index": frame_index,
                        "time_seconds": round(seconds, 3),
                        "latitude": position["latitude"],
                        "longitude": position["longitude"],
                        "altitude_m": position.get("altitude_m"),
                        "gimbal_pitch_deg": position.get("gimbal_pitch_deg"),
                        "estimated_overlap_percent": round(overlap, 1) if overlap is not None else None,
                        "turn_angle_deg": round(turn_change, 1) if turn_change is not None else None,
                        "selection_reason": reason,
                    })
                    anchor_telemetry = position
                    anchor_to_previous = np.eye(3, dtype=np.float64)
                elif anchor_to_current is not None:
                    anchor_to_previous = anchor_to_current
                previous_features = current_features
        frame_index += 1
    capture.release()
    result = {
        "video_fps": fps,
        "video_frames": frame_index,
        "duration_seconds": round(frame_index / fps, 2),
        "scan_fps": args.scan_fps,
        "scanned_candidates": scanned,
        "selected_frames": selected,
        "reached_limit": len(selected) >= args.max_frames and frame_index < expected_frame_count,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "strategy": "SRT course change + visual feature homography overlap + gimbal change; no fixed output frame interval",
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
