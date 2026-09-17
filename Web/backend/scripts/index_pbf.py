"""Build a local, spatially indexed road database from a Jiangsu OSM PBF.

Run once after downloading the PBF. Route requests query the resulting SQLite
file; they never rescan the province file or call an online map service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import osmium


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from road_planner import ROAD_KINDS  # noqa: E402


DEFAULT_SOURCE = Path(__file__).resolve().parents[3] / "jiangsu-260916.osm.pbf"
DEFAULT_OUTPUT = BACKEND / "data" / "jiangsu_roads.sqlite"


class RoadIndexBuilder(osmium.SimpleHandler):
    def __init__(self, connection: sqlite3.Connection):
        super().__init__()
        self.connection = connection
        self.roads: list[tuple] = []
        self.road_bounds: list[tuple] = []
        self.buildings: list[tuple] = []
        self.building_bounds: list[tuple] = []
        self.road_id = 0
        self.building_id = 0

    def way(self, way: osmium.osm.Way) -> None:
        tags = {tag.k: tag.v for tag in way.tags}
        is_road = tags.get("highway") in ROAD_KINDS and tags.get("area") != "yes"
        is_building = "building" in tags
        if not is_road and not is_building:
            return

        nodes = [
            (node.ref, node.location.lon, node.location.lat) if node.location.valid() else None
            for node in way.nodes
        ]
        if is_road:
            for a, b in zip(nodes, nodes[1:]):
                if a is None or b is None:
                    continue
                if a[0] == b[0] or (a[1], a[2]) == (b[1], b[2]):
                    continue
                self.road_id += 1
                self.roads.append((self.road_id, a[0], b[0], a[1], a[2], b[1], b[2], way.id, tags.get("name"), tags["highway"]))
                self.road_bounds.append((self.road_id, min(a[1], b[1]), max(a[1], b[1]), min(a[2], b[2]), max(a[2], b[2])))

        if is_building and len(nodes) >= 4 and all(node is not None for node in nodes) and nodes[0][0] == nodes[-1][0]:
            coordinates = [[lon, lat] for _, lon, lat in nodes]
            self.building_id += 1
            self.buildings.append((self.building_id, json.dumps(coordinates, separators=(",", ":"))))
            self.building_bounds.append((self.building_id, min(p[0] for p in coordinates), max(p[0] for p in coordinates), min(p[1] for p in coordinates), max(p[1] for p in coordinates)))

        if len(self.roads) >= 10_000 or len(self.buildings) >= 2_000:
            self.flush()

    def flush(self) -> None:
        if self.roads:
            self.connection.executemany(
                "INSERT INTO roads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", self.roads
            )
            self.connection.executemany(
                "INSERT INTO road_rtree VALUES (?, ?, ?, ?, ?)", self.road_bounds
            )
            self.roads.clear()
            self.road_bounds.clear()
        if self.buildings:
            self.connection.executemany("INSERT INTO buildings VALUES (?, ?)", self.buildings)
            self.connection.executemany(
                "INSERT INTO building_rtree VALUES (?, ?, ?, ?, ?)", self.building_bounds
            )
            self.buildings.clear()
            self.building_bounds.clear()
        self.connection.commit()


def build_index(source: Path, output: Path) -> tuple[int, int]:
    if not source.is_file():
        raise FileNotFoundError(f"找不到 PBF 文件：{source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="road_index_", suffix=".sqlite", dir=output.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)

    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.executescript("""
                PRAGMA journal_mode=MEMORY;
                PRAGMA synchronous=OFF;
                CREATE TABLE roads (
                    id INTEGER PRIMARY KEY, a INTEGER NOT NULL, b INTEGER NOT NULL,
                    a_lon REAL NOT NULL, a_lat REAL NOT NULL,
                    b_lon REAL NOT NULL, b_lat REAL NOT NULL,
                    way_id INTEGER NOT NULL, name TEXT, highway TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE road_rtree USING rtree(id, min_lon, max_lon, min_lat, max_lat);
                CREATE TABLE buildings (id INTEGER PRIMARY KEY, coordinates TEXT NOT NULL);
                CREATE VIRTUAL TABLE building_rtree USING rtree(id, min_lon, max_lon, min_lat, max_lat);
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            builder = RoadIndexBuilder(connection)
            payload = source.read_bytes()
            builder.apply_buffer(payload, "pbf", locations=True)
            builder.flush()
            connection.executemany("INSERT INTO metadata VALUES (?, ?)", [
                ("source_file", source.name),
                ("source_sha256", hashlib.sha256(payload).hexdigest()),
                ("indexed_at_utc", datetime.now(timezone.utc).isoformat()),
                ("road_segments", str(builder.road_id)),
                ("buildings", str(builder.building_id)),
            ])
            connection.commit()
        os.replace(temporary_path, output)
        return builder.road_id, builder.building_id
    finally:
        temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    roads, buildings = build_index(arguments.source, arguments.output)
    print(f"已建立 {arguments.output}：{roads} 段道路、{buildings} 个建筑轮廓")
