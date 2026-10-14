from __future__ import annotations

import io
import zipfile
from argparse import Namespace
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from waterwatch.reproduce_caltech_rgbt import (
    Pair,
    decode_mask,
    fit_models,
    load_pair,
    official_pairs,
    run,
    score_split,
    trajectory_split,
    zip_member_index,
)


def _encoded(extension: str, image: np.ndarray) -> bytes:
    good, result = cv2.imencode(extension, image)
    assert good
    return result.tobytes()


def _source_mask() -> np.ndarray:
    mask = np.full((64, 80), 2, np.uint8)
    mask[:, 40:] = 9
    mask[:4, :] = 0
    mask[4:8, :] = 1
    return mask


def test_indexed_source_mask_maps_water_and_ignores_unknown_background() -> None:
    stream = io.BytesIO()
    Image.fromarray(_source_mask()).convert("P").save(stream, format="PNG")
    mapped = decode_mask(stream.getvalue())
    assert set(np.unique(mapped)) == {0, 1, 255}
    assert mapped[0, 45] == 255
    assert mapped[5, 45] == 255
    assert mapped[30, 20] == 0
    assert mapped[30, 60] == 1

    invalid = _source_mask()
    invalid[20, 20] = 99
    stream = io.BytesIO()
    Image.fromarray(invalid).save(stream, format="PNG")
    with pytest.raises(ValueError, match="Unexpected Caltech labels"):
        decode_mask(stream.getvalue())


def test_official_mask_key_pairing_and_three_mode_common_support(tmp_path: Path) -> None:
    split_dir = tmp_path / "splits"
    split_dir.mkdir()
    for split, stems in {"train": ["a", "b"], "val": ["c"], "test": ["d"]}.items():
        rgb_lines = [f"color/{stem}_eo-00001.jpg,annotations/{stem}_mask-00001.png" for stem in stems]
        thermal_lines = [f"thermal16/{stem}_thermal-00001.tiff,annotations/{stem}_mask-00001.png"
                         for stem in reversed(stems)]
        (split_dir / f"rgb_{split}.txt").write_text("\n".join(rgb_lines) + "\n", encoding="utf-8")
        (split_dir / f"thermal16_{split}.txt").write_text("\n".join(thermal_lines) + "\n", encoding="utf-8")
    splits, hashes = official_pairs(split_dir)
    assert [len(splits[key]) for key in ("train", "val", "test")] == [2, 1, 1]
    assert len(hashes) == 6
    assert splits["train"][0].rgb.startswith("color/a_")
    assert splits["train"][0].thermal.startswith("thermal16/a_")

    zip_path = tmp_path / "mini.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for pair in sum(splits.values(), []):
            rgb = np.zeros((64, 80, 3), np.uint8)
            rgb[:, :40] = (5, 190, 30)
            rgb[:, 40:] = (220, 40, 10)
            thermal = np.full((64, 80), 12000, np.uint16)
            thermal[:, 40:] = 24000
            thermal[:, :5] = 0  # RGB must also exclude thermal padding.
            archive.writestr("labeled_rgbt_pairs/" + pair.rgb, _encoded(".jpg", rgb))
            archive.writestr("labeled_rgbt_pairs/" + pair.thermal, _encoded(".tiff", thermal))
            stream = io.BytesIO()
            Image.fromarray(_source_mask()).convert("P").save(stream, format="PNG")
            archive.writestr("labeled_rgbt_pairs/" + pair.mask, stream.getvalue())

    with zipfile.ZipFile(zip_path) as archive:
        members = zip_member_index(archive)
        scene = load_pair(archive, members, splits["test"][0], 80)
        assert scene.thermal is not None and scene.thermal.dtype == np.float32
        assert scene.thermal_valid is not None and not scene.thermal_valid[:, :5].any()
        models, counts = fit_models(archive, members, splits["train"], max_edge=80,
                                    pixels_per_class=20, trees=4, seed=11, jobs=1)
        assert counts["class_counts"]["0"] > 0
        assert counts["class_counts"]["1"] > 0
        assert all(bundle["classifier"].n_jobs == 1 for bundle in models.values())
        report = score_split(archive, members, splits["test"], models, 80,
                             examples_dir=tmp_path / "examples")
        assert report["common_evaluated_pixels"] == 56 * 75
        assert set(report["per_mode"]) == {"rgb", "thermal", "fusion"}
        for mode in ("rgb", "thermal", "fusion"):
            assert sum(sum(row) for row in report["per_mode"][mode]["metrics"]["confusion_rows_true_columns_predicted"]) == 56 * 75
        example = tmp_path / "examples" / "d_mask-00001"
        # imdecode/fromfile also works when the Windows temporary path has Chinese characters.
        def decoded(name: str) -> np.ndarray:
            raw = np.fromfile(str(example / name), dtype=np.uint8)
            image = cv2.imdecode(raw, cv2.IMREAD_UNCHANGED)
            assert image is not None
            return image

        assert decoded("input_rgb.png").shape == (64, 80, 3)
        assert decoded("input_thermal.png").shape == (64, 80)
        assert decoded("rgb.png").shape == (64, 80)


def test_official_splits_reject_overlap(tmp_path: Path) -> None:
    for split in ("train", "val", "test"):
        for modality, folder in (("rgb", "color"), ("thermal16", "thermal16")):
            (tmp_path / f"{modality}_{split}.txt").write_text(
                f"{folder}/same.png,annotations/same_mask-00001.png\n", encoding="utf-8")
    with pytest.raises(ValueError, match="overlap"):
        official_pairs(tmp_path)


def test_trajectory_split_covers_all_images_without_group_overlap() -> None:
    official: dict[str, list[Pair]] = {"train": [], "val": [], "test": []}
    all_keys = set()
    for group, count in enumerate((20, 15, 13, 11, 10, 8, 7, 5, 4, 3)):
        for frame in range(count):
            old_split = ("train", "val", "test")[frame % 3]
            key = f"annotations/flight{group}_mask-{frame:05d}.png"
            all_keys.add(key)
            official[old_split].append(Pair(
                key=key, split=old_split,
                rgb=f"color/flight{group}_eo-{frame:05d}.jpg",
                thermal=f"thermal16/flight{group}_thermal-{frame:05d}.tiff",
                mask=key,
            ))
    first = trajectory_split(official, 42)
    second = trajectory_split(official, 42)
    assert {name: [pair.key for pair in pairs] for name, pairs in first.items()} == {
        name: [pair.key for pair in pairs] for name, pairs in second.items()
    }
    assert {pair.key for pairs in first.values() for pair in pairs} == all_keys
    groups = {name: {pair.trajectory for pair in pairs} for name, pairs in first.items()}
    assert all(first[name] for name in first)
    assert not (groups["train"] & groups["val"])
    assert not (groups["train"] & groups["test"])
    assert not (groups["val"] & groups["test"])
    assert all(pair.split == name for name, pairs in first.items() for pair in pairs)


def test_jobs_must_be_positive_before_accessing_archive() -> None:
    with pytest.raises(ValueError, match="jobs must be positive"):
        run(Namespace(max_edge=256, pixels_per_class=60, trees=32, jobs=0))
