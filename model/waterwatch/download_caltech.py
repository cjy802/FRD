"""Resume-safe downloader for CaltechDATA's paired aerial RGB/thermal ZIP.

The API endpoint issues a fresh, short-lived S3 redirect for every request.
Never persist or reuse the redirected URL. Downloaded data is not Git content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

SOURCE = (
    "https://data.caltech.edu/api/records/cks6g-ps927/"
    "files/labeled_rgbt_pairs.zip/content"
)
EXPECTED_SIZE = 4_287_170_398
EXPECTED_MD5 = "22f42923694e724d4b7e354bace12389"
SPLIT_COMMIT = "dc1adecf72f58f0191e337321c20ede2ace0e49f"
SPLIT_ROOT = (
    "https://raw.githubusercontent.com/aerorobotics/"
    f"caltech-aerial-rgbt-dataset/{SPLIT_COMMIT}/splits/rgbt_splits"
)
SPLIT_COUNTS = {"train": 1731, "val": 272, "test": 279}
MODALITIES = ("rgb", "thermal8", "thermal16")
_THREAD_LOCAL = threading.local()


def _session() -> requests.Session:
    if not hasattr(_THREAD_LOCAL, "session"):
        _THREAD_LOCAL.session = requests.Session()
    return _THREAD_LOCAL.session


def _reset_session() -> None:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is not None:
        session.close()
        del _THREAD_LOCAL.session


def _fetch_range(index: int, offset: int, size: int) -> bytes:
    started = time.monotonic()
    deadline_seconds = 90 if size <= 2 * 1024 * 1024 else 180
    end = offset + size - 1
    expected_range = f"bytes {offset}-{end}/{EXPECTED_SIZE}"
    # The API endpoint is intentional: it issues a fresh signed redirect for each
    # individual request, including fallback subranges.
    response = _session().get(
        SOURCE,
        headers={"Range": f"bytes={offset}-{end}", "Cache-Control": "no-cache"},
        timeout=(20, 45),
        stream=True,
        allow_redirects=True,
    )
    with response:
        if time.monotonic() - started > deadline_seconds:
            raise requests.Timeout(f"chunk {index}: range exceeded {deadline_seconds}s")
        if response.status_code != 206:
            raise RuntimeError(f"chunk {index}: HTTP {response.status_code}")
        if response.headers.get("Content-Range") != expected_range:
            raise RuntimeError(
                f"chunk {index}: unexpected Content-Range "
                f"{response.headers.get('Content-Range')!r}"
            )
        data = bytearray()
        for piece in response.iter_content(1024 * 1024):
            data.extend(piece)
            if time.monotonic() - started > deadline_seconds:
                raise requests.Timeout(f"chunk {index}: range exceeded {deadline_seconds}s")
            if len(data) > size:
                raise RuntimeError(f"chunk {index}: response longer than expected")
        if len(data) != size:
            raise RuntimeError(f"chunk {index}: short response {len(data)} != {size}")
        return bytes(data)


def _download_chunk(index: int, offset: int, size: int, retries: int) -> tuple[int, bytes, str]:
    try:
        result = _fetch_range(index, offset, size)
        return index, result, hashlib.sha256(result).hexdigest()
    except (requests.RequestException, OSError, RuntimeError) as exc:
        if isinstance(exc, requests.RequestException):
            _reset_session()
        print(
            f"[retry] chunk {index} 8 MiB range: {type(exc).__name__}; "
            "switching to 2 MiB subranges",
            flush=True,
        )

    # Keep successful 2 MiB pieces in memory while retrying only the piece that
    # failed. This matters when the remote server drops some long responses.
    pieces: list[bytes] = []
    for piece_number, sub_offset in enumerate(range(offset, offset + size, 2 * 1024 * 1024), 1):
        sub_size = min(2 * 1024 * 1024, offset + size - sub_offset)
        for attempt in range(retries + 1):
            try:
                pieces.append(_fetch_range(index, sub_offset, sub_size))
                break
            except (requests.RequestException, OSError, RuntimeError) as exc:
                if isinstance(exc, requests.RequestException):
                    _reset_session()
                print(
                    f"[retry] chunk {index} part {piece_number} "
                    f"attempt {attempt + 1}/{retries + 1}: {type(exc).__name__}",
                    flush=True,
                )
                if attempt == retries:
                    raise RuntimeError(
                        f"chunk {index} part {piece_number} failed after "
                        f"{retries + 1} attempts: {type(exc).__name__}"
                    ) from exc
                time.sleep(min(2 ** attempt, 15))
    result = b"".join(pieces)
    if len(result) != size:
        raise RuntimeError(f"chunk {index}: assembled {len(result)} bytes, expected {size}")
    return index, result, hashlib.sha256(result).hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, sort_keys=True)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def _digest(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as file:
        for block in iter(lambda: file.read(16 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _resume_state(part: Path, progress: Path, chunk_size: int) -> dict:
    expected = {
        "source": SOURCE,
        "size": EXPECTED_SIZE,
        "expected_md5": EXPECTED_MD5,
        "chunk_size": chunk_size,
    }
    if progress.exists():
        state = json.loads(progress.read_text(encoding="utf-8"))
        if any(state.get(key) != value for key, value in expected.items()):
            raise RuntimeError("progress metadata differs from this download; inspect it manually")
        if not part.exists() or part.stat().st_size != EXPECTED_SIZE:
            raise RuntimeError("partial file is absent or has wrong length")
    else:
        if part.exists():
            raise RuntimeError("partial file exists without progress metadata; inspect it manually")
        free = shutil.disk_usage(part.parent).free
        if free < EXPECTED_SIZE + 1024 * 1024 * 1024:
            raise RuntimeError(f"insufficient free space: {free} bytes")
        with part.open("wb") as file:
            file.truncate(EXPECTED_SIZE)
            file.flush()
            os.fsync(file.fileno())
        state = {**expected, "completed": {}}
        _atomic_json(progress, state)

    # A completed marker is trusted only if its bytes still match the saved hash.
    verified = {}
    with part.open("rb") as file:
        for key, value in state["completed"].items():
            index = int(key)
            offset = index * chunk_size
            if index < 0 or offset >= EXPECTED_SIZE or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise RuntimeError(f"invalid progress entry for chunk {key}")
            file.seek(offset)
            data = file.read(min(chunk_size, EXPECTED_SIZE - offset))
            if hashlib.sha256(data).hexdigest() == value:
                verified[key] = value
    if verified != state["completed"]:
        print(f"Re-downloading {len(state['completed']) - len(verified)} damaged chunks", flush=True)
        state["completed"] = verified
        _atomic_json(progress, state)
    return state


def download(output: Path, workers: int, chunk_size: int, retries: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        actual = _digest(output, "md5") if output.stat().st_size == EXPECTED_SIZE else "wrong size"
        if actual == EXPECTED_MD5:
            print(f"Already verified: {output} (MD5 {actual})", flush=True)
            return
        raise RuntimeError(f"existing output is invalid: {output}, MD5/size {actual}")

    part = output.with_name(output.name + ".part")
    progress = output.with_name(output.name + ".progress.json")
    state = _resume_state(part, progress, chunk_size)
    total = (EXPECTED_SIZE + chunk_size - 1) // chunk_size
    remaining = [
        i for i in range(total) if str(i) not in state["completed"]
    ]
    print(
        f"Resuming {output}: {len(state['completed'])}/{total} chunks complete; "
        f"{workers} workers, {chunk_size / 1024**2:g} MiB/chunk",
        flush=True,
    )
    initial_bytes = sum(
        min(chunk_size, EXPECTED_SIZE - int(key) * chunk_size)
        for key in state["completed"]
    )
    started = time.monotonic()
    last_log = started
    error = None
    with part.open("r+b", buffering=0) as file, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _download_chunk,
                index,
                index * chunk_size,
                min(chunk_size, EXPECTED_SIZE - index * chunk_size),
                retries,
            ): index
            for index in remaining
        }
        try:
            for future in as_completed(futures):
                index, data, sha256 = future.result()
                file.seek(index * chunk_size)
                file.write(data)
                os.fsync(file.fileno())
                state["completed"][str(index)] = sha256
                _atomic_json(progress, state)
                now = time.monotonic()
                done = len(state["completed"])
                if now - last_log >= 20 or done == total:
                    completed_bytes = sum(
                        min(chunk_size, EXPECTED_SIZE - int(key) * chunk_size)
                        for key in state["completed"]
                    )
                    speed = (completed_bytes - initial_bytes) / max(now - started, 1) / 1024**2
                    print(
                        f"{done}/{total} chunks, {completed_bytes / 1024**3:.2f}/"
                        f"{EXPECTED_SIZE / 1024**3:.2f} GiB, "
                        f"session mean {speed:.2f} MiB/s",
                        flush=True,
                    )
                    last_log = now
        except Exception as exc:
            error = exc
            for future in futures:
                future.cancel()
    if error is not None:
        raise error
    if len(state["completed"]) != total:
        raise RuntimeError("download ended with missing chunks")

    print("All chunks received; checking official MD5...", flush=True)
    actual_md5 = _digest(part, "md5")
    if actual_md5 != EXPECTED_MD5:
        raise RuntimeError(f"MD5 mismatch: {actual_md5} != {EXPECTED_MD5}")
    with zipfile.ZipFile(part) as archive:
        names = archive.namelist()
        if not names or not any(name.startswith("labeled_rgbt_pairs/") for name in names):
            raise RuntimeError("ZIP layout is unexpected")
        print(f"ZIP central directory valid: {len(names)} entries", flush=True)
    os.replace(part, output)
    progress.unlink()
    print(f"Verified: {output} ({EXPECTED_SIZE} bytes, MD5 {actual_md5})", flush=True)


def fetch_splits(output: Path, splits_dir: Path) -> None:
    """Fetch the exact paired-image splits and validate them against the ZIP."""
    if not output.is_file() or output.stat().st_size != EXPECTED_SIZE:
        raise RuntimeError("the paired ZIP is absent or has the wrong size")
    fetched: dict[str, bytes] = {}
    parsed: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    for split, expected_count in SPLIT_COUNTS.items():
        for modality in MODALITIES:
            name = f"{modality}_{split}.txt"
            url = f"{SPLIT_ROOT}/{name}"
            response = requests.get(url, timeout=(20, 40))
            response.raise_for_status()
            raw = response.content
            lines = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip()]
            if len(lines) != expected_count:
                raise RuntimeError(f"{name}: expected {expected_count} rows, got {len(lines)}")
            images: list[str] = []
            masks: list[str] = []
            image_prefix = "color/" if modality == "rgb" else f"{modality}/"
            for line in lines:
                fields = [field.strip() for field in line.split(",")]
                if len(fields) != 2:
                    raise RuntimeError(f"{name}: invalid row {line!r}")
                image, mask = fields
                if not image.startswith(image_prefix) or not mask.startswith("annotations/"):
                    raise RuntimeError(f"{name}: unexpected path {line!r}")
                images.append(image)
                masks.append(mask)
            if len(set(masks)) != expected_count or len(set(images)) != expected_count:
                raise RuntimeError(f"{name}: duplicate images or masks")
            fetched[name] = raw
            parsed[(modality, split)] = (images, masks)

    for split in SPLIT_COUNTS:
        reference = parsed[("rgb", split)][1]
        for modality in MODALITIES[1:]:
            if parsed[(modality, split)][1] != reference:
                raise RuntimeError(f"{split}: {modality} mask order differs from RGB")
    mask_sets = [set(parsed[("rgb", split)][1]) for split in SPLIT_COUNTS]
    if sum(len(mask_set) for mask_set in mask_sets) != len(set.union(*mask_sets)):
        raise RuntimeError("a mask occurs in more than one split")

    with zipfile.ZipFile(output) as archive:
        members = set(archive.namelist())
    root = "labeled_rgbt_pairs/"
    missing = [
        root + path
        for images, masks in parsed.values()
        for path in images + masks
        if root + path not in members
    ]
    if missing:
        raise RuntimeError(f"split files reference {len(missing)} missing ZIP members; first {missing[0]}")

    splits_dir.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, raw in fetched.items():
        destination = splits_dir / name
        hashes[name] = hashlib.sha256(raw).hexdigest()
        if destination.is_file() and destination.read_bytes() == raw:
            continue
        temporary = destination.with_name(destination.name + ".tmp")
        with temporary.open("wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, destination)
    manifest = {
        "repository": "https://github.com/aerorobotics/caltech-aerial-rgbt-dataset",
        "commit": SPLIT_COMMIT,
        "source_directory": SPLIT_ROOT,
        "counts": SPLIT_COUNTS,
        "sha256": hashes,
    }
    _atomic_json(splits_dir / "source.json", manifest)
    print(
        f"Verified {len(fetched)} pinned split files in {splits_dir}; "
        f"counts {SPLIT_COUNTS}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--chunk-mib", type=int, default=8)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--splits-dir", type=Path, default=None)
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        parser.error("workers must be between 1 and 32")
    if not 1 <= args.chunk_mib <= 64:
        parser.error("chunk-mib must be between 1 and 64")
    if not 0 <= args.retries <= 20:
        parser.error("retries must be between 0 and 20")
    download(args.output, args.workers, args.chunk_mib * 1024 * 1024, args.retries)
    splits_dir = args.splits_dir or args.output.parent / "caltech_splits"
    fetch_splits(args.output, splits_dir)


if __name__ == "__main__":
    main()
