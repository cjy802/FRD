"""Isolated read-only reproduction; the diagnostic parent survives native crashes."""
import argparse
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path('D:/anaconda/envs/fuxi_onnx/python.exe')


def child(mode):
    sys.path.insert(0, str(ROOT / 'app'))
    from prepare_gfs_height import decode_region
    paths = [(ROOT / 'app/prepared/gfs_aux_cache/nomads_2026100818_f006.grib2', datetime(2026, 10, 9, tzinfo=timezone.utc)),
             (ROOT / 'app/prepared/gfs_aux_cache/nomads_2026100818_f012.grib2', datetime(2026, 10, 9, 6, tzinfo=timezone.utc))]
    print(f'START {mode}', flush=True)
    if mode == 'parallel':
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda args: decode_region(*args), paths * 10))
    else:
        results = [decode_region(*args) for args in paths * 10]
    assert len(results) == 20
    print(f'OK {mode}', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', choices=('parallel', 'serial'))
    args = parser.parse_args()
    if args.child:
        child(args.child)
        return
    for mode in ('parallel', 'serial', 'parallel'):
        result = subprocess.run([str(PYTHON), '-X', 'faulthandler', '-B', str(Path(__file__).resolve()), '--child', mode],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        print(mode, 'returncode', result.returncode, 'hex', hex(result.returncode & 0xffffffff), flush=True)
        print(result.stdout[-500:], flush=True)
        print(result.stderr[-6000:], flush=True)
    sys.path.insert(0, str(ROOT / 'app'))
    # A fresh, isolated cache forces all five regional downloads and the native
    # decoding path; it cannot accidentally pass by only reading warm JSON.
    script = ROOT / 'app/prepare_gfs_height.py'
    with tempfile.TemporaryDirectory(prefix='height_cold_', dir=ROOT / 'output') as scratch:
        result = subprocess.run([str(PYTHON), '-X', 'faulthandler', '-B', str(script), '--input',
                                 'D:/DATASET/FuXi_RuntimeLinks/FuXiApp/prepared/latest.nc', '--cache-dir', scratch],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600)
        import json
        print('COLD PROVIDER', result.returncode, hex(result.returncode & 0xffffffff), 'stdout bytes', len(result.stdout), flush=True)
        print(result.stderr[-4000:], flush=True)
        payload = json.loads(result.stdout)
        assert result.returncode == 0 and 'error' not in payload, payload
        print('COLD VERIFIED', payload['init_time_utc'], payload['source_cycle_utc'],
              [(frame['lead_hours'], frame['transport']) for frame in payload['snapshots']], flush=True)


if __name__ == '__main__': main()
