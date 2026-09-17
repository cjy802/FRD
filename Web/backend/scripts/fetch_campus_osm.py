"""Refresh the small campus OSM extract used by the local route demo."""

from pathlib import Path
from urllib.request import Request, urlopen


BBOX = "119.9477,31.6783,119.9526,31.6810"
URL = f"https://api.openstreetmap.org/api/0.6/map?bbox={BBOX}"
TARGET = Path(__file__).resolve().parents[1] / "data" / "campus.osm"


def main() -> None:
    request = Request(URL, headers={"User-Agent": "LowAltitudeRoutePrototype/0.1 (local campus demonstration)"})
    with urlopen(request, timeout=25) as response:
        payload = response.read()
    if b"<osm" not in payload or len(payload) > 2_000_000:
        raise ValueError("Unexpected OSM response")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(payload)
    print(f"Saved {TARGET} ({len(payload)} bytes)")


if __name__ == "__main__":
    main()
