from __future__ import annotations

import numpy as np
from PIL import Image

from waterwatch.build_replay_mix import build_mix, sample_stems


def _make_domain(root, name: str, stems: list[str], mask_suffix: str = ".png") -> dict:
    images = root / f"{name}_images"
    masks = root / f"{name}_masks"
    images.mkdir(parents=True, exist_ok=True)
    masks.mkdir(parents=True, exist_ok=True)
    for stem in stems:
        Image.fromarray(np.full((8, 8), 200, np.uint8)).save(images / f"{stem}.png")
        if mask_suffix == ".png":
            mask = np.zeros((8, 8), np.uint8)
            mask[2:6, 2:6] = 1
            Image.fromarray(mask, mode="L").save(masks / f"{stem}.png")
        else:
            np.save(masks / f"{stem}.npy", np.zeros((8, 8), np.uint8))
    return {"images": images, "masks": masks}


def _sources(root) -> dict:
    puddle = _make_domain(root, "puddle", [f"p{i}" for i in range(5)])
    fred = _make_domain(root, "fred", [f"f{i}" for i in range(4)])
    localmix = _make_domain(root, "localmix", ["fred_copy"] + [f"local_x{i}" for i in range(3)])
    localval = _make_domain(root, "localval", ["only_photo"], mask_suffix=".npy")
    return {
        "puddle": {**puddle, "prefix": "puddle", "train_k": 3, "val": True},
        "fred": {**fred, "prefix": "fred", "train_k": 0, "val": True},
        "local": {**localmix, "prefix": "local", "train_k": 0, "val": False, "only_prefix": "local_"},
        "localval": {**localval, "prefix": "vlocal", "train_k": 0, "val": True},
    }


def test_sample_stems_edge_cases() -> None:
    stems = ["a", "b", "c", "d"]
    assert sample_stems(stems, 0, __import__("random").Random(42)) == stems
    assert sample_stems(stems, 10, __import__("random").Random(42)) == stems
    assert len(sample_stems(stems, 2, __import__("random").Random(42))) == 2
    assert sample_stems(stems, 2, __import__("random").Random(42)) == sample_stems(stems, 2, __import__("random").Random(42))


def test_build_mix_counts_prefixes_and_determinism(tmp_path) -> None:
    outs = []
    for run in range(2):
        output = tmp_path / f"out{run}"
        report = build_mix(_sources(tmp_path), output, seed=42, val_per_domain=2)
        outs.append((report, sorted(p.name for p in (output / "train" / "images").iterdir()),
                     sorted(p.name for p in (output / "val" / "images").iterdir())))
    report, train_names, val_names = outs[0]
    assert report["domains"]["puddle"]["train"] == 3
    assert report["domains"]["fred"]["train"] == 4
    assert report["domains"]["local"]["train"] == 3  # local_* only, fred_copy excluded
    assert report["domains"]["localval"]["val"] == 1
    assert all(name.startswith("puddle_") for name in train_names if "puddle" in name)
    assert not any("fred_copy" in name for name in train_names)
    assert sum(1 for name in val_names if name.startswith("vlocal_")) == 1
    # masks written as 0/1 PNG regardless of source encoding
    mask = np.asarray(Image.open(tmp_path / "out0/val/masks/vlocal_only_photo.png"))
    assert set(np.unique(mask)).issubset({0, 1})
    # same seed -> identical file sets
    assert outs[0][1] == outs[1][1] and outs[0][2] == outs[1][2]
