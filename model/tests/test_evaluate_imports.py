from waterwatch import evaluate_abcdwavenet


def test_evaluate_module_imports_and_defaults():
    assert evaluate_abcdwavenet.IMAGE_SIZE == 256
    assert callable(evaluate_abcdwavenet.load_image)
