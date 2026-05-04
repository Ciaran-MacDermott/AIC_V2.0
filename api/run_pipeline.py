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
parent for the log box.  Exit codes are defined as ExitCode below
and consumed by api.worker._spawn_pipeline.
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


# ── Exit codes ───────────────────────────────────────────────────────────────
# Shared with api.worker so both sides agree on the contract.

class ExitCode:
    OK             = 0
    ERROR          = 1
    USAGE          = 2
    REVIEW_NEEDED  = 42   # Phase 2A only — mismatches surfaced
    STOPPED        = 99   # SIGTERM observed mid-pipeline


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

def _safe_run(tmpdir: Path, fn, *, stopped_excs: tuple = ()) -> int:
    """
    Run ``fn(tmpdir)`` returning a clean ExitCode.  Maps cooperative-stop
    exceptions to STOPPED, anything else to ERROR (with the exception
    pickled into error.pkl so the parent can classify() it).  Mismatch
    handling is per-command so it lives in the caller.
    """
    try:
        return fn(tmpdir)
    except stopped_excs:
        return ExitCode.STOPPED
    except BaseException as exc:
        traceback.print_exc()
        _dump_error(tmpdir, exc)
        return ExitCode.ERROR


def _cmd_phase1(tmpdir: Path) -> int:
    from api.pipeline import run_phase1, PipelineStopped

    def _do(td: Path) -> int:
        args = _load_pkl(td / "input.pkl")
        payload = run_phase1(args["excel_path"], args["csv_path"], stop_event=_STOP)
        _dump_pkl(td / "result.pkl", payload)
        return ExitCode.OK

    return _safe_run(tmpdir, _do, stopped_excs=(PipelineStopped,))


def _cmd_phase2_a(tmpdir: Path) -> int:
    from api.pipeline_phase2 import (
        MismatchReviewNeeded,
        PipelineStopped,
        run_phase_a,
    )

    def _do(td: Path) -> int:
        args = _load_pkl(td / "input.pkl")
        try:
            interim = run_phase_a(
                Path(args["directory_path"]),
                args["phase2_inputs"],
                stop_event=_STOP,
            )
        except MismatchReviewNeeded as exc:
            # Not a failure — Phase 2A's documented "needs user review"
            # path.  Stash the interim state so Phase 2B can resume.
            _dump_pkl(td / "interim.pkl", exc.phase_a_state)
            _dump_pkl(td / "mismatch.pkl", exc.groups)
            return ExitCode.REVIEW_NEEDED
        _dump_pkl(td / "interim.pkl", interim)
        return ExitCode.OK

    return _safe_run(tmpdir, _do, stopped_excs=(PipelineStopped,))


def _cmd_phase2_b(tmpdir: Path) -> int:
    from api.pipeline_phase2 import PipelineStopped, run_phase_b

    def _do(td: Path) -> int:
        interim = _load_pkl(td / "interim.pkl")
        corrections = _load_pkl(td / "corrections.pkl")
        result = run_phase_b(
            interim, corrections, output_dir=td, stop_event=_STOP,
        )
        _dump_pkl(td / "result.pkl", result)
        return ExitCode.OK

    return _safe_run(tmpdir, _do, stopped_excs=(PipelineStopped,))


def _cmd_post_qc(tmpdir: Path) -> int:
    from phase3_package.pipeline import run_post_qc

    def _do(td: Path) -> int:
        args = _load_pkl(td / "input.pkl")
        _, category_splits = run_post_qc(
            excel_path=args["edited_xlsx_path"],
            is_custom_collapse=args["is_custom_collapse"],
        )
        _dump_pkl(td / "result.pkl", category_splits)
        return ExitCode.OK

    # Post-QC has no cooperative-stop check — the run is short and the
    # legacy code doesn't take a stop_event.  Errors still get caught.
    return _safe_run(tmpdir, _do)


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
        return ExitCode.USAGE
    cmd, tmpdir = sys.argv[1], Path(sys.argv[2])
    return _COMMANDS[cmd](tmpdir)


if __name__ == "__main__":
    sys.exit(main())
