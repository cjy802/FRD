"""The two independent labeling tasks. Ignore value 255 is shared by both."""

from __future__ import annotations

CLASSES: dict[str, dict[int, str]] = {
    "ponding": {0: "dry", 1: "wet_without_standing_water", 2: "suspected_ponding"},
    "shoreline": {0: "land", 1: "visible_water"},
}

TARGET_CLASS = {"ponding": 2, "shoreline": 1}
MODES = ("rgb", "thermal", "fusion")
IGNORE_LABEL = 255
