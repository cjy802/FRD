"""Download the author's public 700-pair Floodwater manual ZIP with resume.

The public Nextcloud endpoint sometimes drops long transfers, so this script
requests bounded byte ranges and verifies the resulting ZIP before renaming it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import time
import urllib.request
import zipfile
from pathlib import Path


SHARE_ID = "8ZsKAa7yRtznRjY"
URL = f"https://cloud.ilabt.imec.be/public.php/dav/files/{SHARE_ID}/?accept=zip"
EXPECTED_BYTES = 132_896_325  # Observed for the public ZIP on 2026-10-14.


def _read_range(start: int, end: int, timeout: int) -> bytes:
    authorization = base64.b64encode(f"{SHARE_ID}:".encode("ascii")).decode("ascii")
    request = urllib.request.Request(
        URL,
        headers={"Authorization": f"Basic {authorization}", "Range": f"bytes={start}-{end}"},
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


def download(output: Path, *, chunk_mib: int = 8, attempts: int = 6, timeout: int = 30) -> dict:
    if chunk_mib < 1 or attempts < 1:
        raise ValueError("chunk_mib and attempts must be positive")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    part = output.with_name(output.name + ".part")
    if output.is_file():
        with zipfile.ZipFile(output) as archive:
            if archive.testzip() is not None:
                raise ValueError("Existing ZIP failed CRC check")
        return {"path": str(output), "bytes": output.stat().st_size, "sha256": _sha256(output)}
    offset = part.stat().st_size if part.exists() else 0
    if offset > EXPECTED_BYTES:
        raise ValueError("Partial file is larger than the expected archive")
    with part.open("ab") as stream:
        while offset < EXPECTED_BYTES:
            end = min(offset + chunk_mib * 1024 * 1024, EXPECTED_BYTES) - 1
            for retry in range(attempts):
                try:
                    data = _read_range(offset, end, timeout)
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
    with zipfile.ZipFile(part) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise ValueError(f"ZIP member failed CRC check: {corrupt}")
    digest = _sha256(part)
    part.replace(output)
    return {"path": str(output), "bytes": output.stat().st_size, "sha256": digest}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/external/floodwater_manual.zip"))
    parser.add_argument("--chunk-mib", type=int, default=8)
    args = parser.parse_args()
    print(download(args.output, chunk_mib=args.chunk_mib), flush=True)


if __name__ == "__main__":
    main()
