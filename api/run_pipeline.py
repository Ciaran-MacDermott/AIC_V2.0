"""
CLI entry-point for running a pipeline stage in an isolated subprocess.

This is what the worker thread spawns instead of calling the pipeline
functions in-process.  Subprocess isolation matters because both
ml_package and phase3_package mutate global state (os.chdir,
sys.path) mid-pipeline — two simultaneous in-process runs would
corrupt each other.  One process per run side-steps that entirely.

Usage:
    python -m api.run_pipeline {phase1|phase2_a|phase2_b|post_qc} <tmpdir>

The tmpdir holds both inputs (input.pkl) and outputs (result.pkl,
interim.pkl, mismatch.pkl, error.pkl).  Stdout streams live to the
parent for the log box.

Exit codes:
    0   success
    42  Phase 2A produced mismatches needing user review
    99  pipeline received SIGTERM (user pressed Stop)
    1   any other exception (traceback dumped to stderr, exception
        pickled to error.pkl for the parent to classify)
"""

from __future__ import annotations

import os
import pickle
import signal
import sys
import threading
import traceback
from pathlib import Path

# Force unbuffered IO so the parent's line-by-line stdout reader sees
# stage banners as soon as they're printed.  Belt-and-braces with the
# -u flag the parent passes.
os.environ.setdefault("PYTHONUNBUFFERED", "1")

# Mirror the parent's NLTK bootstrap before importing anything that
# might touch the corpora.  api/__init__.py does this on import.
import api  # noqa: F401


# ── Stop-event plumbing ──────────────────────────────────────────────────────
# The pipeline functions take a threading.Event for cooperative stop checks.
# Bridge SIGTERM (sent by the parent) into that event.
_STOP = threading.Event()


def _on_term(_signum, _frame):
    _STOP.set()


signal.signal(signal.SIGTERM, _on_term)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_pkl(path: Path):
    with open(path, "rb") as f:
        return pickle.load(f)


def _dump_pkl(path: Path, obj) -> None:
    with open(path, "wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)


def _dump_error(tmpdir: Path, exc: BaseException) -> None:
    """Pickle the exception so the parent can call classify(exc) on it."""
    try:
        _dump_pkl(tmpdir / "error.pkl", exc)
    except Exception:
        # Some exceptions (e.g. with unpicklable __cause__) won't round-trip;
        # fall back to a plain RuntimeError carrying the message.
        _dump_pkl(tmpdir / "error.pkl", RuntimeError(str(exc)))


# ── Commands ─────────────────────────────────────────────────────────────────

def _cmd_phase1(tmpdir: Path) -> int:
    from api.pipeline import run_phase1, PipelineStopped
    args = _load_pkl(tmpdir / "input.pkl")
    try:
        payload = run_phase1(args["excel_path"], args["csv_path"], stop_event=_STOP)
    except PipelineStopped:
        return 99
    except BaseException as exc:
        traceback.print_exc()
        _dump_error(tmpdir, exc)
        return 1
    _dump_pkl(tmpdir / "result.pkl", payload)
    return 0


def _cmd_phase2_a(tmpdir: Path) -> int:
    from api.pipeline_phase2 import (
        MismatchReviewNeeded,
        PipelineStopped,
        run_phase_a,
    )
    args = _load_pkl(tmpdir / "input.pkl")
    try:
        interim = run_phase_a(
            Path(args["directory_path"]),
            args["phase2_inputs"],
            stop_event=_STOP,
        )
    except MismatchReviewNeeded as exc:
        _dump_pkl(tmpdir / "interim.pkl", exc.phase_a_state)
        _dump_pkl(tmpdir / "mismatch.pkl", exc.groups)
        return 42
    except PipelineStopped:
        return 99
    except BaseException as exc:
        traceback.print_exc()
        _dump_error(tmpdir, exc)
        return 1
    _dump_pkl(tmpdir / "interim.pkl", interim)
    return 0


def _cmd_phase2_b(tmpdir: Path) -> int:
    from api.pipeline_phase2 import PipelineStopped, run_phase_b
    interim = _load_pkl(tmpdir / "interim.pkl")
    corrections = _load_pkl(tmpdir / "corrections.pkl")
    try:
        result = run_phase_b(
            interim, corrections, output_dir=tmpdir, stop_event=_STOP,
        )
    except PipelineStopped:
        return 99
    except BaseException as exc:
        traceback.print_exc()
        _dump_error(tmpdir, exc)
        return 1
    _dump_pkl(tmpdir / "result.pkl", result)
    return 0


def _cmd_post_qc(tmpdir: Path) -> int:
    from phase3_package.pipeline import run_post_qc
    args = _load_pkl(tmpdir / "input.pkl")
    try:
        _, category_splits = run_post_qc(
            excel_path=args["edited_xlsx_path"],
            is_custom_collapse=args["is_custom_collapse"],
        )
    except BaseException as exc:
        traceback.print_exc()
        _dump_error(tmpdir, exc)
        return 1
    _dump_pkl(tmpdir / "result.pkl", category_splits)
    return 0


_COMMANDS = {
    "phase1":   _cmd_phase1,
    "phase2_a": _cmd_phase2_a,
    "phase2_b": _cmd_phase2_b,
    "post_qc":  _cmd_post_qc,
}


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in _COMMANDS:
        print(
            "usage: python -m api.run_pipeline {phase1|phase2_a|phase2_b|post_qc} <tmpdir>",
            file=sys.stderr,
        )
        return 2
    cmd, tmpdir = sys.argv[1], Path(sys.argv[2])
    return _COMMANDS[cmd](tmpdir)


if __name__ == "__main__":
    sys.exit(main())
