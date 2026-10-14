from __future__ import annotations

import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from waterwatch.reproduce_floodwater import (
    cross_split_exact_duplicates,
    decode_mask,
    load_pair,
    sequential_split,
    stems_in_archive,
)


def _encoded(extension: str, image: np.ndarray) -> bytes:
    good, encoded = cv2.imencode(extension, image)
    assert good
    return encoded.tobytes()


def test_source_water_label_255_becomes_project_water_label_1() -> None:
    source_mask = np.array([[0, 255], [255, 0]], dtype=np.uint8)
    converted = decode_mask(_encoded(".png", source_mask))
    assert converted.tolist() == [[0, 1], [1, 0]]
    with pytest.raises(ValueError, match="Unexpected"):
        decode_mask(_encoded(".png", np.array([[0, 128]], dtype=np.uint8)))


def test_zip_pairing_alignment_and_split_leakage_check(tmp_path: Path) -> None:
    path = tmp_path / "mini.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for number in range(1, 5):
            stem = f"sample_{number:06d}"
            bgr = np.full((80, 100, 3), number * 20, dtype=np.uint8)
            source_mask = np.zeros((80, 100), dtype=np.uint8)
            source_mask[:, 50:] = 255
            archive.writestr(f"floodwater_manual/images/{stem}.jpg", _encoded(".jpg", bgr))
            archive.writestr(f"floodwater_manual/masks/{stem}.png", _encoded(".png", source_mask))
    with zipfile.ZipFile(path) as archive:
        stems = stems_in_archive(archive)
        scene = load_pair(archive, stems[0], 64)
        assert scene.rgb.shape[:2] == scene.mask.shape == (51, 64)
        assert set(np.unique(scene.mask)) == {0, 1}
    splits = sequential_split(stems, train_count=2, val_count=1)
    assert [len(splits[key]) for key in ("train", "val", "test")] == [2, 1, 1]
    hashes = {stem: stem for stem in stems}
    assert cross_split_exact_duplicates(splits, hashes) == {
        "train_val": 0,
        "train_test": 0,
        "val_test": 0,
    }
    hashes[stems[-1]] = hashes[stems[0]]
    assert cross_split_exact_duplicates(splits, hashes)["train_test"] == 1
