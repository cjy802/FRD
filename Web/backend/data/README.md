# Campus map extract

`campus.osm` is an OpenStreetMap XML extract for bounding box `119.9477,31.6783,119.9526,31.6810`, retrieved on 2026-09-16 from the [OpenStreetMap API](https://wiki.openstreetmap.org/wiki/Api06). It contains OSM contributors' data under the [Open Database License](https://www.openstreetmap.org/copyright). The route demo reads this local snapshot and does not call OSM at request time.

The extract is for a small campus prototype. `python scripts/fetch_campus_osm.py` refreshes it manually. The OSM editing API is not intended for bulk data downloads; for larger areas, use an appropriate extract provider or Overpass service and follow their usage policies.

`jiangsu_roads.sqlite` is built locally from `jiangsu-260916.osm.pbf` by
`python scripts/index_pbf.py --source ..\..\jiangsu-260916.osm.pbf`. It stores
road segments and simple building outlines with spatial indexes. Route requests
query only the A/B area plus a buffer. The PBF and SQLite index are local data
artifacts; neither is a rendered basemap. Rebuild the index after replacing the
PBF. The route API uses this index when present and falls back to `campus.osm`
otherwise.
