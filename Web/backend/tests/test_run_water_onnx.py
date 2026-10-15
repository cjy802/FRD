import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np


def load_runner_module():
    """Load the numeric helpers without requiring the waterseg environment."""
    module_name = "run_water_onnx_test_target"
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "run_water_onnx.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load {module_path}")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"cv2": types.ModuleType("cv2"), "onnxruntime": types.ModuleType("onnxruntime")}):
        spec.loader.exec_module(module)
    return module


class SegmentationProbabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner_module()

    def test_probabilities_normalize_classes_not_image_rows(self):
        logits = np.asarray(
            [
                [
                    [[2.0, 0.0], [0.0, 2.0]],
                    [[0.0, 2.0], [2.0, 0.0]],
                    [[-1.0, -1.0], [-1.0, -1.0]],
                ]
            ]
        )

        probabilities = self.runner.class_probabilities(logits)
        self.assertEqual(probabilities.shape, (3, 2, 2))
        self.assertTrue(np.allclose(probabilities.sum(axis=0), 1.0))

        # The previous call shape, softmax(logits[0]), normalizes rows and
        # therefore produces a different confidence for the water class.
        previous = self.runner.softmax(logits[0])
        self.assertFalse(np.allclose(probabilities, previous))

    def test_probabilities_reject_non_batched_logits(self):
        with self.assertRaisesRegex(ValueError, r"\[1, classes, height, width\]"):
            self.runner.class_probabilities(np.zeros((2, 3, 4)))


if __name__ == "__main__":
    unittest.main()
