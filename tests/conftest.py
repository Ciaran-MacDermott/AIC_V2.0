"""
Stub heavy ML deps before api.pipeline gets imported.

The real ml_package needs numpy/xgboost/openpyxl which aren't worth
installing for unit tests of the BFF layer. We replace them with no-op
stubs so api.pipeline (and therefore the FastAPI app) can be imported
in isolation. Tests that exercise the actual pipeline live in a separate
module marked with `pytest.mark.slow` (out of scope here).
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

# Tests monkeypatch the in-process pipeline functions on api.worker
# (run_phase1, run_phase_a, run_phase_b, run_post_qc).  In production
# the worker spawns subprocesses that wouldn't pick up those patches —
# AIC_INPROCESS=1 routes the worker through the legacy in-process path
# so the existing test stubs keep working.
os.environ.setdefault("AIC_INPROCESS", "1")

# Make refactor/ root importable.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# Try real pandas first — it's installed for the BFF tests and pulls in a
# real numpy. Only fall back to stubs if the dep simply isn't there.
try:
    import pandas  # noqa: F401
except ImportError:
    _install_stub("numpy", __version__="0.0-stub")
    _install_stub(
        "pandas",
        DataFrame=type("DataFrame", (), {}),
        Series=type("Series", (), {}),
    )

# Prefer the real openpyxl when it's installed — the integration suite
# needs it for end-to-end fixture writes. Fall back to a stub for the
# fast unit tests on machines that haven't installed the heavy stack.
try:
    import openpyxl  # noqa: F401
except ImportError:
    _install_stub("openpyxl", load_workbook=lambda *a, **kw: None)

# Stub the ml_package modules so api.pipeline can import without the
# actual ML deps installed.
ml_pkg = _install_stub("ml_package")
_install_stub(
    "ml_package.mapping_lookup",
    runLookup=lambda *a, **kw: ({}, None, None, {}),
)
_install_stub(
    "ml_package.text_match",
    runTextMatch=lambda *a, **kw: None,
)
_install_stub(
    "ml_package.xgb_classifier",
    runML=lambda *a, **kw: {},
)
_install_stub(
    "ml_package.ensemble",
    runEnsemble=lambda *a, **kw: {},
)
_install_stub(
    "ml_package.write_results",
    write_results=lambda *a, **kw: None,
)
