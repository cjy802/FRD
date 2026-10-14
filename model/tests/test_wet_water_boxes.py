from __future__ import annotations

import zipfile
from io import BytesIO

import cv2
import numpy as np
import pytest

from waterwatch.reproduce_wet_water_boxes import (
    Box,
    box_pixels,
    crop_features,
    inspect_archive,
    parse_yolo_labels,
    split_images,
)


def _jpeg(bgr: np.ndarray) -> bytes:
    success, encoded = cv2.imencode(".jpeg", bgr)
    assert success
    return encoded.tobytes()


def test_yolo_centers_class_ids_and_bad_boxes() -> None:
    boxes, dropped = parse_yolo_labels(
        b"0 0.5 0.5 1 1\n1 0.25 0.25 0.5 0.5\n0 0.8 0.5 0 0.1\n",
        "labels.txt",
    )
    assert [box.label for box in boxes] == [0, 1]
    assert dropped == 1
    image = np.arange(256 * 256 * 3, dtype=np.uint32).reshape(256, 256, 3).astype(np.uint8)
    assert box_pixels(image, boxes[0]).shape[:2] == (256, 256)
    assert box_pixels(image, boxes[1]).shape[:2] == (128, 128)
    assert np.isfinite(crop_features(box_pixels(image, boxes[1]))).all()
    with pytest.raises(ValueError, match="beyond"):
        parse_yolo_labels(b"0 0.95 0.5 0.2 0.2", "bad.txt")
    with pytest.raises(ValueError, match="unexpected class"):
        parse_yolo_labels(b"2 0.5 0.5 0.2 0.2", "bad.txt")


def test_duplicate_hash_canonical_label_and_image_level_split() -> None:
    archive_bytes = BytesIO()
    root = "Stagnant water and Wet surface Dataset/"
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr(root + "classes.txt", "water\nwet surface\n")
        first_image = None
        for number in range(20):
            color = np.array([number * 11, 100, 200 - number * 7], dtype=np.uint8)
            image = np.broadcast_to(color, (256, 256, 3)).copy()
            raw = _jpeg(image)
            if number == 0:
                first_image = raw
            stem = f"image{number:02d}"
            archive.writestr(root + stem + ".jpeg", raw)
            label = number % 2
            archive.writestr(root + stem + ".txt", f"{label} 0.5 0.5 0.5 0.5\n")
        assert first_image is not None
        archive.writestr(root + "image99.jpeg", first_image)
        archive.writestr(root + "image99.txt", "1 0.5 0.5 0.5 0.5\n0 0.5 0.5 0 0.2\n")

    archive_bytes.seek(0)
    with zipfile.ZipFile(archive_bytes) as archive:
        records, stats = inspect_archive(archive)
    assert stats["source_images"] == 21
    assert stats["source_zero_area_boxes_dropped"] == 1
    assert stats["canonical_images"] == 20
    assert stats["duplicate_groups_with_conflicting_box_labels"] == 1
    assert stats["duplicate_groups_with_conflicting_class_presence"] == 1
    assert all(record.path != root + "image99.jpeg" for record in records)
    split = split_images(records, seed=42)
    assert {key: len(value) for key, value in split.items()} == {
        "train": 14, "validation": 3, "test": 3,
    }
    paths = [record.path for group in split.values() for record in group]
    assert len(paths) == len(set(paths)) == 20
    digests = [record.digest for group in split.values() for record in group]
    assert len(digests) == len(set(digests))
