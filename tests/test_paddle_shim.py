"""Tests for the PaddlePredictorOption compat shim (paddleocr/paddlex API drift)."""
import sys
import types

import process_receipts as pr


def _fake_common_args(monkeypatch, option_cls):
    """Install a fake paddleocr._common_args module exposing option_cls."""
    mod = types.ModuleType("paddleocr._common_args")
    mod.PaddlePredictorOption = option_cls
    monkeypatch.setitem(sys.modules, "paddleocr._common_args", mod)
    # Block the secondary patch target so the real paddlex (when installed)
    # isn't imported or mutated by these tests.
    monkeypatch.setitem(sys.modules, "paddlex.inference", types.ModuleType("paddlex.inference"))
    return mod


class _KwOnlyOption:
    """paddlex >= 3.1 style: keyword-only init that rejects unknown options."""

    SUPPORTED = {"device_type", "device_id"}

    def __init__(self, **kwargs):
        for k in kwargs:
            if k not in self.SUPPORTED:
                raise Exception(f"{k} is not supported to set!")
        self.cfg = kwargs


class _StrictKwOnlyOption:
    """Rejects every option — forces the shim down to the defaults-only rung."""

    def __init__(self, **kwargs):
        if kwargs:
            raise Exception("nothing is supported to set!")
        self.cfg = {}


class _LegacyOption:
    """paddlex 3.0 style: accepts a positional model_name."""

    def __init__(self, model_name=None, **kwargs):
        self.model_name = model_name
        self.cfg = kwargs


def test_shim_drops_rejected_positional_model_name(monkeypatch):
    mod = _fake_common_args(monkeypatch, _KwOnlyOption)
    pr._patch_paddle_predictor_option()
    opt = mod.PaddlePredictorOption("PP-OCRv4_det", device_type="cpu", device_id=None)
    assert opt.cfg == {"device_type": "cpu", "device_id": None}


def test_shim_falls_back_to_defaults_when_all_kwargs_rejected(monkeypatch):
    mod = _fake_common_args(monkeypatch, _StrictKwOnlyOption)
    pr._patch_paddle_predictor_option()
    opt = mod.PaddlePredictorOption("PP-OCRv4_det", device_type="cpu", device_id=None)
    assert opt.cfg == {}


def test_shim_skips_compatible_class(monkeypatch):
    mod = _fake_common_args(monkeypatch, _LegacyOption)
    pr._patch_paddle_predictor_option()
    assert mod.PaddlePredictorOption is _LegacyOption  # untouched
    opt = mod.PaddlePredictorOption("PP-OCRv4_det", device_type="cpu")
    assert opt.model_name == "PP-OCRv4_det"


def test_shim_is_idempotent(monkeypatch):
    mod = _fake_common_args(monkeypatch, _KwOnlyOption)
    pr._patch_paddle_predictor_option()
    patched_once = mod.PaddlePredictorOption
    pr._patch_paddle_predictor_option()
    assert mod.PaddlePredictorOption is patched_once  # not re-wrapped


def test_shim_handles_missing_modules(monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr._common_args", None)
    monkeypatch.setitem(sys.modules, "paddlex.inference", None)
    pr._patch_paddle_predictor_option()  # must not raise


def test_reset_paddle_engine_failure_clears_cached_failure(monkeypatch):
    monkeypatch.setattr(pr, "_paddle_engine", False)
    pr._reset_paddle_engine_failure()
    assert pr._paddle_engine is None


def test_reset_paddle_engine_failure_keeps_working_engine(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(pr, "_paddle_engine", sentinel)
    pr._reset_paddle_engine_failure()
    assert pr._paddle_engine is sentinel
