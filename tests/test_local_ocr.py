"""Tests for the local OCR engine wrapper (RapidOCR), engine fully mocked.

No real RapidOCR install is required: these exercise the singleton lifecycle and
the result parsing for both RapidOCR APIs (rapidocr-onnxruntime's (result, elapse)
tuple and the newer unified rapidocr object with .txts).
"""
import process_receipts as pr


# ── Engine singleton lifecycle ─────────────────────────────────────────────────

def test_get_ocr_engine_disabled(monkeypatch):
    monkeypatch.setattr(pr, "LOCAL_OCR_ENABLED", False)
    monkeypatch.setattr(pr, "_ocr_engine", None)
    assert pr._get_ocr_engine() is None


def test_get_ocr_engine_caches_failure(monkeypatch):
    monkeypatch.setattr(pr, "LOCAL_OCR_ENABLED", True)
    monkeypatch.setattr(pr, "_ocr_engine", False)  # prior init failure
    assert pr._get_ocr_engine() is None


def test_reset_ocr_engine_failure_clears_cached_failure(monkeypatch):
    monkeypatch.setattr(pr, "_ocr_engine", False)
    pr._reset_ocr_engine_failure()
    assert pr._ocr_engine is None


def test_reset_ocr_engine_failure_keeps_working_engine(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(pr, "_ocr_engine", sentinel)
    pr._reset_ocr_engine_failure()
    assert pr._ocr_engine is sentinel


# ── Result parsing across both RapidOCR APIs ───────────────────────────────────

def test_rapidocr_lines_onnxruntime_tuple_form():
    """rapidocr-onnxruntime returns (result, elapse); result = [[box, text, score], ...]."""
    result = [
        [[[0, 0], [1, 0], [1, 1], [0, 1]], "SHELL", 0.99],
        [[[0, 2], [1, 2], [1, 3], [0, 3]], "TOTAL $45.20", 0.98],
    ]
    assert pr._rapidocr_lines((result, 0.5)) == ["SHELL", "TOTAL $45.20"]


def test_rapidocr_lines_none_result():
    assert pr._rapidocr_lines((None, 0.1)) == []


def test_rapidocr_lines_skips_empty_text():
    result = [[[[0, 0]], "", 0.9], [[[0, 1]], "OK", 0.9]]
    assert pr._rapidocr_lines((result, 0.0)) == ["OK"]


def test_rapidocr_lines_unified_object_form():
    """Newer unified rapidocr package returns an object exposing .txts."""
    class _Out:
        txts = ("SHELL", "TOTAL $45.20", "")
    assert pr._rapidocr_lines(_Out()) == ["SHELL", "TOTAL $45.20"]


# ── _extract_local_ocr wrapper ─────────────────────────────────────────────────

def test_extract_local_ocr_joins_lines(monkeypatch, tmp_path):
    img = tmp_path / "r.png"
    img.write_bytes(b"fake")
    result = [[[[0, 0]], "SHELL", 0.99], [[[0, 2]], "TOTAL $45.20", 0.98]]
    monkeypatch.setattr(pr, "_get_ocr_engine", lambda: (lambda path: (result, 0.3)))
    assert pr._extract_local_ocr(img) == "SHELL\nTOTAL $45.20"


def test_extract_local_ocr_returns_none_when_engine_unavailable(monkeypatch, tmp_path):
    img = tmp_path / "r.png"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_get_ocr_engine", lambda: None)
    assert pr._extract_local_ocr(img) is None


def test_extract_local_ocr_returns_none_when_no_text(monkeypatch, tmp_path):
    img = tmp_path / "r.png"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_get_ocr_engine", lambda: (lambda path: (None, 0.1)))
    assert pr._extract_local_ocr(img) is None


def test_extract_local_ocr_handles_engine_exception(monkeypatch, tmp_path):
    img = tmp_path / "r.png"
    img.write_bytes(b"fake")

    def _boom(path):
        raise RuntimeError("inference blew up")

    monkeypatch.setattr(pr, "_get_ocr_engine", lambda: _boom)
    assert pr._extract_local_ocr(img) is None
