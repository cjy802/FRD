"""End-to-end smoke test for the regional corrector chain (synthetic data)."""
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Web" / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from weather_service import load_corrector, point_model  # noqa: E402
from regional_weather import RegionalWeather  # noqa: E402

FIELDS = ("t2m_K", "u10_m_s", "v10_m_s", "msl_Pa", "tp_6h_mm")
rng = np.random.default_rng(7)
lats = [35.0, 34.75, 34.5]
lons = [116.0, 116.25, 116.5]
rows = []
for day in range(120):
    init = datetime(2025, 1, 1, 6, tzinfo=timezone.utc) + timedelta(days=day)
    for lead in (6, 12, 24):
        valid = init + timedelta(hours=lead)
        doy = valid.timetuple().tm_yday + valid.hour / 24.0
        for lat in lats:
            for lon in lons:
                cell_bias = 0.8 * (lat - 34.7) + 1.2 * (lon - 116.25)
                fuxi = {
                    "t2m_K": 288 + 8 * np.sin(2 * np.pi * doy / 365.25) + rng.normal(0, .3),
                    "u10_m_s": 3 + rng.normal(0, .5),
                    "v10_m_s": -1 + rng.normal(0, .5),
                    "msl_Pa": 101300 + rng.normal(0, 80),
                    "tp_6h_mm": np.clip(rng.gamma(1.2, 1.0), 0, None),
                }
                era5 = {field: fuxi[field] + cell_bias + rng.normal(0, .4) for field in FIELDS}
                row = {
                    "init_time_utc": init.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "valid_time_utc": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "lead_hours": lead, "latitude_deg": lat, "longitude_deg": lon,
                    "model": "smoke",
                }
                row.update({f"fuxi_{f}": fuxi[f] for f in FIELDS})
                row.update({f"era5_{f}": era5[f] for f in FIELDS})
                rows.append(row)
table = pd.DataFrame(rows)

with tempfile.TemporaryDirectory(prefix="region_smoke_") as work:
    work = Path(work)
    data = work / "pairs.csv"
    out = work / "fit"
    table.to_csv(data, index=False)
    completed = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve().parent.parent / "Web" / "mode" / "fit_region_corrector.py"),
         "--data", str(data), "--output-dir", str(out), "--min-rows", "40"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    if completed.returncode:
        print(completed.stdout[-3000:])
        print(completed.stderr[-3000:])
        raise SystemExit("trainer failed")
    model = load_corrector(out / "corrector_region_ridge.npz")
    assert "latitudes" in model and model["latitudes"].shape == (3,)
    assert model["trained_leads"] == (6, 12, 24)
    assert point_model(model, 34.75, 116.25) is not None
    print("trainer + loader OK; cells trained:", int(np.isfinite(model["coefficients"]).all(axis=(2, 3)).sum()))

    channels = ["T2M", "U10", "V10", "MSL", "TP"]
    init = datetime(2025, 5, 1, 6, tzinfo=timezone.utc)
    snapshots = []
    for lead in (0, 6, 12, 18, 24):
        valid = init + timedelta(hours=lead)
        values = rng.normal(size=(5, len(lats), len(lons)))
        values[0] += 288
        values[3] += 101300
        values[4] = np.abs(values[4])
        snapshots.append({"valid_time_utc": valid.isoformat(), "lead_hours": lead,
                          "values": values.tolist(), "source": "initial_background" if lead == 0 else "fuxi"})
    grid = RegionalWeather({"latitudes": lats, "longitudes": lons, "channels": channels, "snapshots": snapshots},
                           init.isoformat(), "smoke")
    result = grid.sample(34.75, 116.25, model, "2025-05-01T12:00:00Z")
    assert result["corrector_applicable"] is True
    assert result["sources"]["t2m_K"] == "ridge" and result["sources"]["v10_m_s"] == "fuxi"
    off = grid.sample(34.63, 116.37, model, "2025-05-01T12:00:00Z")
    assert off["corrector_applicable"] is True
    lead18 = grid.sample(34.75, 116.25, model, (init + timedelta(hours=18)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert lead18["corrector_applicable"] is False, "lead 18 must stay uncorrected"
    interp = grid.sample(34.75, 116.25, model, (init + timedelta(hours=15)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert interp["corrector_applicable"] is False, "time interpolation must stay uncorrected"
    legacy = grid.sample(34.75, 116.25, None)
    assert legacy["corrector_applicable"] is False and legacy["model_warning"]
    print("sample branch OK: applicable/off-grid corrected, lead18/interpolation/None raw")

print("SMOKE PASS")
