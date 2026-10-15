"""Export trained segmentation networks to ONNX and verify numerical consistency.

ABCDWaveNet (RGB ponding, 1x3x256x256 -> 1x2x256x256 logits) and CMNeXt-B2
(RGB+thermal water, two 1x3x480x640 inputs -> 1x10x480x640 logits) are exported
with a fixed input shape and checked against PyTorch with onnxruntime on random
inputs. Reports max absolute logit deviation; anything above 1e-2 fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _export(bundle_path: Path, onnx_path: Path, dummy_inputs: list[torch.Tensor], model: torch.nn.Module, *, dynamo: bool = False) -> None:
    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    if dynamo:
        # timm CondConv2d computes conv weights dynamically; the legacy tracer
        # cannot export that, the torch 2.7 dynamo exporter can.
        torch.onnx.export(
            model,
            dummy_inputs[0],
            str(onnx_path),
            input_names=[f"input_{index}" for index in range(len(dummy_inputs))],
            output_names=["logits"],
            opset_version=18,
            dynamo=True,
        )
        return
    torch.onnx.export(
        model,
        tuple(dummy_inputs) if len(dummy_inputs) > 1 else dummy_inputs[0],
        str(onnx_path),
        input_names=[f"input_{index}" for index in range(len(dummy_inputs))],
        output_names=["logits"],
        opset_version=17,
        do_constant_folding=True,
    )


def _verify(onnx_path: Path, model: torch.nn.Module, inputs: list[torch.Tensor]) -> dict:
    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    feed = {f"input_{index}": value.numpy() for index, value in enumerate(inputs)}
    onnx_logits = session.run(["logits"], feed)[0]
    with torch.inference_mode():
        torch_logits = (model(*inputs) if len(inputs) > 1 else model(inputs[0])).numpy()
    return {
        "max_abs_logit_deviation": float(np.max(np.abs(onnx_logits - torch_logits))),
        "mean_abs_logit_deviation": float(np.mean(np.abs(onnx_logits - torch_logits))),
    }


def export_abcdwavenet(third_party: Path, checkpoint: Path, output: Path) -> dict:
    repo = third_party / "ABCDWaveNet"
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from model import UNet
    state = torch.load(checkpoint, map_location="cpu")
    if isinstance(state, dict) and "mask_values" in state:
        del state["mask_values"]
    model = UNet(n_channels=3, n_classes=2, bilinear=True)
    model.load_state_dict(state)
    model.eval()
    dummy = torch.randn(1, 3, 256, 256)
    _export(checkpoint, output, [dummy], model, dynamo=True)
    verification = _verify(output, model, [dummy])
    return {
        "model": "abcdwavenet",
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": _sha256(checkpoint),
        "onnx": str(output),
        "onnx_sha256": _sha256(output),
        "input": ["input_0", [1, 3, 256, 256]],
        "output": ["logits", [1, 2, 256, 256], "class 1 = water (argmax or softmax[:,1] threshold)"],
        "verification": verification,
    }


def export_cmnext(third_party: Path, checkpoint: Path, output: Path, image_size: tuple[int, int]) -> dict:
    deliver = third_party / "DELIVER"
    if str(deliver) not in sys.path:
        sys.path.insert(0, str(deliver))
    from semseg.models import CMNeXt

    model = CMNeXt("CMNeXt-B2", 10, ["img", "thermal"])
    state = torch.load(checkpoint, map_location="cpu")
    if isinstance(state, dict) and "model_state_dict" in state:
        state = state["model_state_dict"]
    model.load_state_dict({key.removeprefix("module."): value for key, value in state.items()})
    model.eval()

    class TwoInputWrapper(torch.nn.Module):
        """CMNeXt.forward expects one list argument; expose two tensor inputs for ONNX."""

        def __init__(self, inner: torch.nn.Module) -> None:
            super().__init__()
            self.inner = inner

        def forward(self, img: torch.Tensor, thermal: torch.Tensor) -> torch.Tensor:
            return self.inner([img, thermal])

    wrapper = TwoInputWrapper(model).eval()
    dummy = [torch.randn(1, 3, *image_size), torch.randn(1, 3, *image_size)]
    _export(checkpoint, output, dummy, wrapper)
    verification = _verify(output, wrapper, dummy)
    return {
        "model": "cmnext_b2_rgbt",
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": _sha256(checkpoint),
        "onnx": str(output),
        "onnx_sha256": _sha256(output),
        "input": [["input_0", [1, 3, *image_size]], ["input_1", [1, 3, *image_size]]],
        "input_notes": "input_0 = RGB (ImageNet normalised), input_1 = thermal 3-channel (per-image 1..255 uint8 scaled /255); class 7 = water",
        "output": ["logits", [1, 10, *image_size]],
        "verification": verification,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("abcdwavenet", "cmnext"), required=True)
    parser.add_argument("--third-party", type=Path, default=Path("third_party"))
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-size", type=int, nargs=2, default=[480, 640], metavar=("H", "W"), help="CMNeXt export size")
    args = parser.parse_args()
    torch.manual_seed(0)
    if args.model == "abcdwavenet":
        report = export_abcdwavenet(args.third_party.resolve(), args.checkpoint.resolve(), args.output.resolve())
    else:
        report = export_cmnext(args.third_party.resolve(), args.checkpoint.resolve(), args.output.resolve(), tuple(args.image_size))
    if report["verification"]["max_abs_logit_deviation"] > 1e-2:
        raise ValueError(f"ONNX deviation too large: {report['verification']}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report_path = args.output.with_suffix(".onnx.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
