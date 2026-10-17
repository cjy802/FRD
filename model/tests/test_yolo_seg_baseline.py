from __future__ import annotations

import numpy as np

from waterwatch.export_floodnet import water_mask_from_index
from waterwatch.yolo_seg_baseline import (
    boxes_to_mask,
    count_patches,
    mask_to_polygons,
    paired_stems,
    polygon_to_box,
    polygons_to_mask,
)


def _disk_mask(height: int = 240, width: int = 320, cx: int = 200, cy: int = 90,
               radius: int = 60) -> np.ndarray:
    yy, xx = np.mgrid[0:height, 0:width]
    return (((xx - cx) ** 2 + (yy - cy) ** 2) <= radius**2).astype(np.uint8)


def test_polygon_round_trip_preserves_shape() -> None:
    mask = _disk_mask()
    polygons = mask_to_polygons(mask, min_area=1)
    assert len(polygons) == 1
    rebuilt = polygons_to_mask(polygons, width=320, height=240)
    intersection = int(((mask == 1) & (rebuilt == 1)).sum())
    union = int(((mask == 1) | (rebuilt == 1)).sum())
    assert intersection / union > 0.98


def test_small_speckles_are_dropped() -> None:
    mask = _disk_mask()
    mask[5:8, 5:9] = 1  # 12-pixel speckle
    assert len(mask_to_polygons(mask, min_area=30)) == 1
    assert len(mask_to_polygons(mask, min_area=1)) == 2


def test_empty_mask_gives_no_polygons() -> None:
    assert mask_to_polygons(np.zeros((64, 64), np.uint8)) == []


def test_count_patches_ignores_speckles() -> None:
    mask = np.zeros((100, 100), np.uint8)
    mask[10:30, 10:30] = 1   # 400 px
    mask[50:70, 50:70] = 1   # 400 px
    mask[5:8, 80:83] = 1     # 9 px speckle
    assert count_patches(mask, min_area=50) == 2
    assert count_patches(mask, min_area=1) == 3
    assert count_patches(np.zeros((64, 64), np.uint8)) == 0


def test_floodnet_water_classes() -> None:
    index = np.array([[0, 3, 5], [9, 8, 3], [1, 5, 2]], np.uint8)
    mask = water_mask_from_index(index)
    assert mask.tolist() == [[0, 1, 1], [0, 0, 1], [0, 1, 0]]


def test_box_pseudo_mask_loses_disk_area() -> None:
    # A disk covers pi/4 ~= 0.785 of its bounding box: the measurable cost of
    # rectangle annotations on irregular ponding shapes.
    mask = _disk_mask()
    cx, cy, width, height = polygon_to_box(mask_to_polygons(mask, min_area=1)[0])
    x1, y1 = (cx - width / 2) * 320, (cy - height / 2) * 240
    x2, y2 = (cx + width / 2) * 320, (cy + height / 2) * 240
    pseudo = boxes_to_mask(np.array([[x1, y1, x2, y2]]), width=320, height=240)
    intersection = int(((mask == 1) & (pseudo == 1)).sum())
    union = int(((mask == 1) | (pseudo == 1)).sum())
    assert 0.70 < intersection / union < 0.85


def test_boxes_to_mask_handles_empty_and_bounds() -> None:
    empty = boxes_to_mask(np.zeros((0, 4)), width=100, height=80)
    assert empty.shape == (80, 100) and not empty.any()
    clipped = boxes_to_mask(np.array([[-10.0, -10.0, 500.0, 500.0]]), width=100, height=80)
    assert clipped.all()


def test_paired_stems_matches_image_and_mask(tmp_path) -> None:
    images = tmp_path / "images"
    masks = tmp_path / "masks"
    images.mkdir()
    masks.mkdir()
    (images / "a.png").write_bytes(b"png")
    (images / "b.png").write_bytes(b"png")
    (images / ".hidden.png").write_bytes(b"png")
    (masks / "a.jpg").write_bytes(b"jpg")
    pairs = paired_stems(images, masks)
    assert [stem for stem, _, _ in pairs] == ["a"]
    assert pairs[0][2].name == "a.jpg"
