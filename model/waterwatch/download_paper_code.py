"""Clone and pin both paper codebases, apply compatibility patches, fetch weights.

Delivers under third_party/ (git-ignored, never committed):
- DELIVER (official CMNeXt implementation, CVPR 2023) at a pinned commit plus a
  Caltech RGB-T dataset adapter and config from model/patches/deliver_caltech.patch.
- ABCDWaveNet (official code for arXiv 2504.05112) at a pinned commit plus CLI
  path options from model/patches/abcdwavenet_cli.patch.
- SegFormer MiT-B2 ImageNet backbone used to initialise CMNeXt, pinned by SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path


DELIVER_URL = "https://github.com/jamycheung/DELIVER.git"
DELIVER_COMMIT = "b3a76c33a7b22098b7285d845f22c910abc5cb16"
ABCD_URL = "https://github.com/Lyu-Dakang/ABCDWaveNet.git"
ABCD_COMMIT = "8b1602110c2478f598ea03d8f94c759f84aeda35"
MIT_B2_URL = "https://huggingface.co/InSAI-Lab/CMNeXt/resolve/main/pretrained/segformers/mit_b2.pth"
MIT_B2_SHA256 = "ced22617efb7bae3c34ad0a80f20a9b8afb4d27368cb0835a23456baa9d0e092"
MIT_B2_BYTES = 98_887_027


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}\n{result.stderr}")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_repo(name: str, url: str, commit: str, root: Path, patch: Path, marker: str, marker_content: str | None = None) -> dict:
    target = root / name
    if not (target / ".git").is_dir():
        root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # Existing non-git directory (e.g. only pretrained weights): clone beside it
            # and graft the .git directory in, then materialise tracked files.
            temp = root / f"{name}.clone-tmp"
            if temp.exists():
                shutil.rmtree(temp)
            _run(["git", "-c", "core.autocrlf=false", "clone", "--quiet", url, str(temp)])
            shutil.move(str(temp / ".git"), str(target / ".git"))
            shutil.rmtree(temp)
            _run(["git", "checkout", "-f", "HEAD"], cwd=target)
        else:
            _run(["git", "-c", "core.autocrlf=false", "clone", "--quiet", url, str(target)])
    head = _run(["git", "rev-parse", "HEAD"], cwd=target).stdout.strip()
    if head != commit:
        _run(["git", "checkout", "--quiet", commit], cwd=target)
        head = _run(["git", "rev-parse", "HEAD"], cwd=target).stdout.strip()
    if head != commit:
        raise ValueError(f"{name} HEAD {head} is not the pinned commit {commit}")
    status = _run(["git", "status", "--porcelain"], cwd=target).stdout.strip()
    marker_file = target / marker
    patched = marker_file.is_file() and (marker_content is None or marker_content in marker_file.read_text(encoding="utf-8", errors="replace"))
    if not patched:
        check = subprocess.run(["git", "apply", "--check", str(patch.resolve())], cwd=target, capture_output=True, text=True)
        if check.returncode != 0:
            check = subprocess.run(
                ["git", "apply", "--check", "--ignore-whitespace", str(patch.resolve())],
                cwd=target, capture_output=True, text=True,
            )
            if check.returncode != 0:
                raise RuntimeError(f"patch does not apply to {name} at {commit}:\n{check.stdout}\n{check.stderr}")
            _run(["git", "apply", "--ignore-whitespace", str(patch.resolve())], cwd=target)
        else:
            _run(["git", "apply", str(patch.resolve())], cwd=target)
        marker_file = target / marker
        patched = marker_file.is_file() and (marker_content is None or marker_content in marker_file.read_text(encoding="utf-8", errors="replace"))
        if not patched:
            raise RuntimeError(f"patch applied to {name} but marker {marker} is missing")
    return {"repo": name, "url": url, "commit": head, "patch": patch.name, "patched": patched, "dirty_tree": bool(status)}


def ensure_weight(url: str, expected_sha256: str, expected_bytes: int, target: Path) -> dict:
    if target.is_file():
        digest = _sha256(target)
        if digest != expected_sha256:
            raise ValueError(f"existing {target.name} SHA256 {digest} does not match pinned value")
        return {"file": str(target), "bytes": target.stat().st_size, "sha256": digest, "source": "cached"}
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    offset = part.stat().st_size if part.exists() else 0
    if offset > expected_bytes:
        raise ValueError("partial weight file is larger than expected")
    request_headers = {"User-Agent": "Mozilla/5.0"}
    with part.open("ab") as stream:
        while offset < expected_bytes:
            request = urllib.request.Request(url, headers={**request_headers, "Range": f"bytes={offset}-"})
            for retry in range(5):
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            stream.write(chunk)
                            stream.flush()
                            offset += len(chunk)
                    break
                except OSError as error:
                    if retry + 1 == 5:
                        raise
                    print(f"retry {retry + 1} at offset {offset}: {error}", flush=True)
                    time.sleep(min(2 ** retry, 8))
                    request = urllib.request.Request(url, headers={**request_headers, "Range": f"bytes={offset}-"})
            print(f"downloaded {offset}/{expected_bytes} bytes", flush=True)
    if offset != expected_bytes:
        raise ValueError(f"downloaded {offset} bytes, expected {expected_bytes}")
    digest = _sha256(part)
    if digest != expected_sha256:
        raise ValueError(f"downloaded weight SHA256 {digest} does not match pinned value")
    part.replace(target)
    return {"file": str(target), "bytes": expected_bytes, "sha256": digest, "source": url}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--third-party", type=Path, default=Path("third_party"))
    parser.add_argument("--patches", type=Path, default=Path("patches"))
    args = parser.parse_args()
    root = args.third_party.resolve()
    patches = args.patches.resolve()
    deliver = ensure_repo(
        "DELIVER", DELIVER_URL, DELIVER_COMMIT, root, patches / "deliver_caltech.patch",
        marker=str(Path("semseg/datasets/caltech_rgbt.py")),
    )
    abcd = ensure_repo(
        "ABCDWaveNet", ABCD_URL, ABCD_COMMIT, root, patches / "abcdwavenet_cli.patch",
        marker=str(Path("train.py")), marker_content="apply_runtime_options",
    )
    weight = ensure_weight(
        MIT_B2_URL, MIT_B2_SHA256, MIT_B2_BYTES,
        root / "DELIVER" / "checkpoints" / "pretrained" / "segformer" / "mit_b2.pth",
    )
    report = {
        "deliver": deliver,
        "abcdwavenet": abcd,
        "mit_b2": weight,
        "note": "third_party is git-ignored; rerun this script to re-clone at the pinned commits.",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
