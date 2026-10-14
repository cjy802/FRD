"""Download the audited small Mendeley water/wet-surface RGB box dataset.

This 2,000-image archive is an older file on the version-4 landing page. The
version-4 description refers to a separate cleaned 1,976-image archive.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import time
import urllib.request
import zipfile
from pathlib import Path


URL = "https://data.mendeley.com/public-files/datasets/y6zyrnxbfm/files/abb2d6b7-63cd-4b07-acb5-7ca3e3a87f81/file_downloaded"
EXPECTED_BYTES = 36_555_626
EXPECTED_SHA256 = "84e08c2c2dea792b7b06b431fb898a0392127873b67b5a5d940654c6a84ba117"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_range(start: int, end: int, timeout: int) -> bytes:
    request = urllib.request.Request(
        URL,
        headers={"Range": f"bytes={start}-{end}", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_range = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
        if response.status != 206 or not match:
            raise ValueError(f"Server did not honor byte range: {response.status} {content_range}")
        actual_start, actual_end, total = (int(item) for item in match.groups())
        if (actual_start, actual_end, total) != (start, end, EXPECTED_BYTES):
            raise ValueError(f"Unexpected Content-Range: {content_range}")
        data = response.read()
    if len(data) != end - start + 1:
        raise OSError(f"Incomplete chunk: {len(data)} bytes for {start}-{end}")
    return data


def download(output: Path, *, chunk_mib: int = 4, attempts: int = 6) -> dict:
    if chunk_mib < 1 or attempts < 1:
        raise ValueError("chunk_mib and attempts must be positive")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.stat().st_size != EXPECTED_BYTES or _sha256(output) != EXPECTED_SHA256:
            raise ValueError("Existing archive does not match published file metadata")
        with zipfile.ZipFile(output) as archive:
            if archive.testzip() is not None:
                raise ValueError("Existing ZIP failed CRC check")
        return {"path": str(output), "bytes": EXPECTED_BYTES, "sha256": EXPECTED_SHA256}
    part = output.with_name(output.name + ".part")
    offset = part.stat().st_size if part.exists() else 0
    if offset > EXPECTED_BYTES:
        raise ValueError("Partial file is larger than the expected archive")
    with part.open("ab") as stream:
        while offset < EXPECTED_BYTES:
            end = min(offset + chunk_mib * 1024 * 1024, EXPECTED_BYTES) - 1
            for retry in range(attempts):
                try:
                    data = _read_range(offset, end, timeout=30)
                    break
                except (OSError, ValueError) as error:
                    if retry + 1 == attempts:
                        raise
                    print(f"Retry {retry + 1} for bytes {offset}-{end}: {error}", flush=True)
                    time.sleep(min(2 ** retry, 8))
            stream.write(data)
            stream.flush()
            offset += len(data)
            print(f"Downloaded {offset}/{EXPECTED_BYTES} bytes", flush=True)
    digest = _sha256(part)
    if digest != EXPECTED_SHA256:
        raise ValueError(f"Downloaded archive SHA256 differs from publisher metadata: {digest}")
    with zipfile.ZipFile(part) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise ValueError(f"ZIP member failed CRC check: {corrupt}")
    part.replace(output)
    return {"path": str(output), "bytes": EXPECTED_BYTES, "sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/external/stagnant_water_wet_surface_34mb.zip"))
    parser.add_argument("--chunk-mib", type=int, default=4)
    args = parser.parse_args()
    print(download(args.output, chunk_mib=args.chunk_mib), flush=True)


if __name__ == "__main__":
    main()
