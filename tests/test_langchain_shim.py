"""Tests for the langchain-compat shim that keeps `import paddleocr` alive.

paddlex eagerly imports `langchain.docstore.document` / `langchain.text_splitter`,
which langchain>=1.0 removed. The shim must make those import targets resolve
again without clobbering a healthy install.
"""
import sys
import types

import pytest

import process_receipts as pr


@pytest.fixture()
def clean_langchain():
    """Snapshot and restore any real langchain.* modules around each test."""
    saved = {k: v for k, v in sys.modules.items() if k == "langchain" or k.startswith("langchain.")}
    for k in list(saved):
        del sys.modules[k]
    yield
    for k in [k for k in sys.modules if k == "langchain" or k.startswith("langchain.")]:
        del sys.modules[k]
    sys.modules.update(saved)


def test_shim_provides_paddlex_import_targets(clean_langchain):
    pr._shim_langchain_for_paddle()
    # Exactly the imports paddlex performs at import time — must not raise.
    from langchain.docstore.document import Document
    from langchain.text_splitter import RecursiveCharacterTextSplitter
    assert Document is not None
    assert RecursiveCharacterTextSplitter is not None


def test_shim_is_idempotent(clean_langchain):
    pr._shim_langchain_for_paddle()
    first = sys.modules.get("langchain.docstore.document")
    pr._shim_langchain_for_paddle()
    assert sys.modules.get("langchain.docstore.document") is first  # not re-created


def test_shim_does_not_clobber_a_working_module(clean_langchain):
    real = types.ModuleType("langchain")
    real.__path__ = []
    sub = types.ModuleType("langchain.text_splitter")
    sentinel = object()
    sub.RecursiveCharacterTextSplitter = sentinel
    sys.modules["langchain"] = real
    sys.modules["langchain.text_splitter"] = sub

    pr._shim_langchain_for_paddle()

    # A module that already imports cleanly is left exactly as-is.
    assert sys.modules["langchain.text_splitter"] is sub
    assert sys.modules["langchain.text_splitter"].RecursiveCharacterTextSplitter is sentinel
