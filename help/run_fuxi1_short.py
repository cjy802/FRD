"""Run one FuXi-1.0 +6h step without silently falling back to CPU."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
import xarray as xr

from prepare_fuxi1_input import CHANNELS


def prepare_cuda_libraries() -> list[object]:
    package_root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    names = ("cudnn", "cublas", "cuda_runtime", "cuda_nvrtc", "cufft", "curand", "nvjitlink")
    directories = [package_root / name / "bin" for name in names if (package_root / name / "bin").is_dir()]
    if os.name == "nt":
        os.environ["PATH"] = os.pathsep.join(str(path) for path in directories) + os.pathsep + os.environ["PATH"]
        handles = [os.add_dll_directory(str(path)) for path in directories]
    else:
        handles = []
    if hasattr(ort, "preload_dlls"):
        ort.preload_dlls(directory="")
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("CUDAExecutionProvider is unavailable; refusing a CPU-only global forecast")
    return handles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    if not (args.code_dir / "fuxi.py").is_file():
        raise FileNotFoundError(args.code_dir / "fuxi.py")
    if not (args.model_dir / "short.onnx").is_file():
        raise FileNotFoundError(args.model_dir / "short.onnx")
    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if (args.output_dir / "006.nc").exists():
        raise FileExistsError(f"Refusing to overwrite {args.output_dir / '006.nc'}")

    dll_handles = prepare_cuda_libraries()
    with xr.open_dataarray(args.input) as data:
        if data.shape != (2, 70, 721, 1440):
            raise ValueError(f"FuXi input has wrong shape: {data.shape}")
        if tuple(str(value) for value in data.level.values) != CHANNELS:
            raise ValueError("FuXi input channel order does not match the 70-channel contract")
        if not np.all(np.diff(data.time.values) == np.timedelta64(6, "h")):
            raise ValueError("FuXi input frames must be six hours apart")

        code_file = args.code_dir / "fuxi.py"
        sys.path.insert(0, str(args.code_dir))
        original_argv = sys.argv
        try:
            # The official module parses required CLI arguments during import.
            # Supplying them here prevents argparse from exiting; its __main__
            # block does not run when loaded as fuxi_original.
            sys.argv = [
                str(code_file),
                "--model", str(args.model_dir),
                "--input", str(args.input),
                "--save_dir", str(args.output_dir),
                "--num_steps", "1",
            ]
            spec = importlib.util.spec_from_file_location("fuxi_original", code_file)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Cannot import {code_file}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            sys.argv = original_argv

        original_load_model = module.load_model

        def checked_load_model(model_name: str):
            session = original_load_model(model_name)
            if "CUDAExecutionProvider" not in session.get_providers():
                raise RuntimeError("Model did not activate CUDAExecutionProvider")
            session.disable_fallback()
            print("ONNX Runtime provider:", session.get_providers())
            return session

        module.load_model = checked_load_model
        args.output_dir.mkdir(parents=True, exist_ok=True)
        module.run_inference(str(args.model_dir), data, [1], str(args.output_dir))

    # Keep os.add_dll_directory handles alive until CUDA execution has finished.
    assert dll_handles is not None
    output_path = args.output_dir / "006.nc"
    with xr.open_dataarray(output_path) as output:
        if output.shape != (1, 1, 70, 721, 1440):
            raise ValueError(f"FuXi output has wrong shape: {output.shape}")
        if int(output.step.values[0]) != 6:
            raise ValueError(f"FuXi output step is wrong: {output.step.values}")
        point = output.sel(lat=31.5, lon=119.5)
        print("Point forecast (31.5N, 119.5E):")
        for name in ("T2M", "U10", "V10", "MSL", "TP"):
            print(f"  {name}: {float(point.sel(level=name).values.squeeze())}")
    print(f"READY: {output_path} ({output_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
