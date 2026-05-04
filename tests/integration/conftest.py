"""
Integration-test conftest.

Unlike the fast tests in tests/, integration tests need the *real* ml_package
and phase3_package — they exercise the actual ML pipeline end-to-end against
synthetic fixture data. This conftest does NOT install stubs.

Skips the whole suite cleanly if any heavy dep (pandas/numpy/openpyxl/
xgboost/scikit-learn/nltk/rank-bm25) is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import importlib

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _require(*mods: str) -> None:
    for m in mods:
        pytest.importorskip(m)


# Skip the entire integration suite if a real ML dep is missing — cleaner
# than letting the test fail with an import error mid-collection.
_require(
    "pandas", "numpy", "openpyxl", "xlsxwriter",
    "xgboost", "sklearn", "nltk", "rank_bm25", "rapidfuzz",
)


# Evict any ml_package stubs the fast-test conftest installed and force a
# fresh import of the real package, then reimport every module that already
# captured a reference to the stubbed sub-modules. Without this, api.pipeline
# keeps using the lambdas-returning-empty-dict stubs registered in
# tests/conftest.py.
_STUB_MODULES = (
    "ml_package",
    "ml_package.mapping_lookup",
    "ml_package.text_match",
    "ml_package.xgb_classifier",
    "ml_package.ensemble",
    "ml_package.write_results",
)
for _name in _STUB_MODULES:
    sys.modules.pop(_name, None)
for _name in _STUB_MODULES:
    importlib.import_module(_name)

# api.pipeline binds ml_package functions at import time (`from ml_package
# import ensemble as _ens` etc) — reimport it so those bindings point at the
# real callables, not the leftover stubs.
for _name in ("api.pipeline", "api.worker", "api.main"):
    if _name in sys.modules:
        importlib.reload(sys.modules[_name])


@pytest.fixture(scope="session", autouse=True)
def _ensure_nltk_punkt():
    """
    text_match calls nltk.word_tokenize, which needs the 'punkt_tab' (or
    older 'punkt') tokenizer model. Download once per session so the test
    works on a fresh machine without manual setup.
    """
    import nltk

    for pkg in ("punkt_tab", "punkt"):
        try:
            nltk.data.find(f"tokenizers/{pkg}")
            return
        except LookupError:
            try:
                nltk.download(pkg, quiet=True)
                nltk.data.find(f"tokenizers/{pkg}")
                return
            except (LookupError, Exception):
                continue
    pytest.skip("NLTK tokenizer model unavailable and could not be downloaded")
