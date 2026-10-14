"""Ponding-specific reporting; wet road remains a separate negative class."""

from __future__ import annotations

from typing import Any

import numpy as np

from .config import IGNORE_LABEL


def summarize_ponding(mask: np.ndarray) -> dict[str, Any]:
    valid = mask != IGNORE_LABEL
    return {
        "dry_pixels": int(((mask == 0) & valid).sum()),
        "wet_without_standing_water_pixels": int(((mask == 1) & valid).sum()),
        "suspected_ponding_pixels": int(((mask == 2) & valid).sum()),
        "warning": "潮湿不等于积水；分类结果需要与人工标注和现场照片核对。",
    }
