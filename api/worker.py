"""
Worker thread that orchestrates a pipeline run.

Each pipeline stage runs in a child process (``python -m
api.run_pipeline``) so concurrent runs can't trample each other via
ml_package's chdir / sys.path mutations.  The worker thread is the
parent — it spawns the subprocess, streams its stdout into the
JobRecord's log buffer, and turns the exit code into a state
transition.  Up to MAX_RUN_SLOTS subprocesses run at once; past that,
runs queue at state="queued" and the UI's ETA logic kicks in.

Tests can opt out of subprocessing with AIC_INPROCESS=1 — that path
calls the pipeline functions directly so monkeypatched stubs still
take effect.
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from api.errors import classify
from api.jobs import (
    JobRecord,
    RUN_SLOTS,
    append_log,
    record_run_duration,
    set_state,
)
from api import pipeline as _pipeline_mod
from api.pipeline import (
    PipelineStopped,
    STAGE_LABELS,
    STAGE_PROGRESS,
)
from api.pipeline_phase2 import (
    MismatchReviewNeeded,
    Phase2Inputs,
    PipelineStopped as Phase2PipelineStopped,
    STAGE_LABELS_PHASE2,
    STAGE_PROGRESS_PHASE2,
    collect_dropdown_values,
    run_phase_a,
    run_phase_b,
    serialise_mismatch_groups,
)


# ── Stage stream ─────────────────────────────────────────────────────────────

class _LogStream:
    """
    File-like that funnels print() output into the job's log_lines and
    matches stage transition markers to drive the progress bar.
    """

    def __init__(self, record: JobRecord,
                 progress_table: dict[str, float],
                 label_table: dict[str, str]):
        self._record = record
        self._progress_table = progress_table
        self._label_table = label_table
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self._emit(line)
        return len(text)

    def flush(self) -> None:
        if self._buf.strip():
            self._emit(self._buf.rstrip())
            self._buf = ""

    def fileno(self) -> int:
        # xlsxwriter probes sys.stdout.fileno() under a TTY — make sure
        # any such lookup against this stream fails cleanly.
        raise OSError("_LogStream has no file descriptor")

    def _emit(self, line: str) -> None:
        append_log(self._record, line)
        self._update_stage(line)

    def _update_stage(self, line: str) -> None:
        low = line.lower()
        is_done = "done" in low
        if "start" not in low and not is_done and not any(
            key in low for key in self._progress_table
        ):
            return
        for key, progress in self._progress_table.items():
            if key in low:
                label = self._label_table.get(key, "")
                set_state(
                    self._record,
                    progress=progress,
                    stage_label=("✓ " + label) if is_done else ("⟳ " + label),
                )
                break


# ── Slot management ──────────────────────────────────────────────────────────

@contextmanager
def _run_slot(record: JobRecord):
    """Block on RUN_SLOTS, then yield with the slot held."""
    set_state(record, stage_label="Waiting for a free run slot…")
    RUN_SLOTS.acquire()
    try:
        yield
    finally:
        RUN_SLOTS.release()


# ── Subprocess invocation ────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _spawn_pipeline(record: JobRecord, command: str,
                    progress_table: dict[str, float],
                    label_table: dict[str, str]) -> int:
    """
    Run `python -m api.run_pipeline <command> <tmpdir>` and stream its
    stdout into record's log buffer.  Returns the subprocess exit code.

    Honours record.stop_event by sending SIGTERM (the child catches it
    and exits 99).  Survives monkeypatch via the AIC_INPROCESS escape
    hatch — tests use the in-process branch where the legacy code lives.
    """
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "api.run_pipeline", command, str(record.tmpdir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        cwd=_REPO_ROOT,
    )
    stream = _LogStream(record, progress_table, label_table)

    # Watcher thread converts a stop_event signal into a SIGTERM.
    stop_thread_done = threading.Event()

    def _watch_stop() -> None:
        while not stop_thread_done.is_set():
            if record.stop_event.wait(timeout=0.5):
                try:
                    proc.terminate()
                except Exception:
                    pass
                return

    watcher = threading.Thread(target=_watch_stop, daemon=True,
                               name=f"stop-watch-{record.run_id}")
    watcher.start()

    try:
        assert proc.stdout is not None
        for chunk in proc.stdout:
            stream.write(chunk)
        stream.flush()
    finally:
        stop_thread_done.set()
        try:
            proc.stdout.close()  # type: ignore[union-attr]
        except Exception:
            pass

    return proc.wait()


def _read_pickle(path: Path) -> Any:
    with open(path, "rb") as f:
        return pickle.load(f)


def _classify_subprocess_error(record: JobRecord) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    """
    Read error.pkl (the pickled exception) and run it through classify().
    Falls back to the log tail if the file is missing/unreadable.
    """
    err_path = record.tmpdir / "error.pkl"
    try:
        exc = _read_pickle(err_path)
    except Exception:
        exc = RuntimeError("Pipeline subprocess failed; see log for details.")
    friendly = classify(exc)
    tb = "".join(traceback.format_exception(type(exc), exc, getattr(exc, "__traceback__", None))) or str(exc)
    return tb, friendly.title, friendly.advice, friendly.category


def _write_pickle(path: Path, obj: Any) -> None:
    with open(path, "wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)


# ── In-process fallback (test escape hatch) ─────────────────────────────────

def _inprocess() -> bool:
    return os.environ.get("AIC_INPROCESS") == "1"


def _redirect_stdout(record: JobRecord, progress_table: dict[str, float],
                     label_table: dict[str, str]):
    """Context manager: install _LogStream as stdout/stderr."""
    @contextmanager
    def _ctx():
        stream = _LogStream(record, progress_table, label_table)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = stream
        sys.stderr = stream
        try:
            yield stream
        finally:
            stream.flush()
            sys.stdout, sys.stderr = old_out, old_err
    return _ctx()


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1 worker
# ═══════════════════════════════════════════════════════════════════════════

def run_phase1_worker(record: JobRecord, excel_path: str, csv_path: str) -> None:
    set_state(record, state="running", stage_label="Queued…")
    with _run_slot(record):
        set_state(record, stage_label="Starting…")
        ml_started_at = time.time()
        try:
            if _inprocess():
                with _redirect_stdout(record, STAGE_PROGRESS, STAGE_LABELS):
                    payload = _pipeline_mod.run_phase1(
                        excel_path, csv_path, stop_event=record.stop_event,
                    )
                _attach_phase1_payload(record, payload)
            else:
                _write_pickle(record.tmpdir / "input.pkl", {
                    "excel_path": excel_path, "csv_path": csv_path,
                })
                rc = _spawn_pipeline(record, "phase1", STAGE_PROGRESS, STAGE_LABELS)
                if rc == 99:
                    raise PipelineStopped()
                if rc != 0:
                    tb, title, advice, cat = _classify_subprocess_error(record)
                    set_state(record, state="error", error=tb, stage_label="Failed",
                              error_title=title, error_advice=advice, error_category=cat)
                    return
                payload = _read_pickle(record.tmpdir / "result.pkl")
                _attach_phase1_payload(record, payload)

            set_state(
                record,
                state="qc_ready",
                progress=STAGE_PROGRESS["qc_ready"],
                stage_label=STAGE_LABELS["qc_ready"],
            )
            record_run_duration("phase1", time.time() - ml_started_at)
        except PipelineStopped:
            append_log(record, "Run cancelled by user.")
            set_state(record, state="stopped", stage_label="Stopped")
        except Exception as exc:
            tb = traceback.format_exc()
            for ln in tb.splitlines():
                append_log(record, ln)
            friendly = classify(exc)
            set_state(record, state="error", error=tb, stage_label="Failed",
                      error_title=friendly.title, error_advice=friendly.advice,
                      error_category=friendly.category)


def _attach_phase1_payload(record: JobRecord, payload: Any) -> None:
    with record.lock:
        record.pipeline_payload = {
            "FINAL":         payload.FINAL,
            "FLAT_FILE_OUT": payload.FLAT_FILE_OUT,
            "meta":          payload.meta,
            "dictEnsemble":  payload.dictEnsemble,
        }


def start_phase1(record: JobRecord, excel_path: str, csv_path: str) -> threading.Thread:
    thread = threading.Thread(
        target=run_phase1_worker,
        args=(record, excel_path, csv_path),
        daemon=True,
        name=f"phase1-{record.run_id}",
    )
    thread.start()
    return thread


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 worker
# ═══════════════════════════════════════════════════════════════════════════

def run_phase2_worker(record: JobRecord, directory_path: str,
                      inputs: Phase2Inputs) -> None:
    """
    Phase 2 orchestration.  Three control-flow shapes:

    1. No mismatch (happy path) — Phase A and Phase B both run while the
       slot is held, so we don't churn the semaphore between them.
    2. Mismatch surfaced — drop the slot, park the worker on
       resume_event while the user reviews, reacquire a fresh slot for
       Phase B once corrections arrive.  This means a long review never
       blocks other users.
    3. Error / stop at any point — release slot, mark state, return.
    """
    set_state(record, state="running", stage_label="Queued…")
    phase_a_started_at = time.time()
    review_seconds = 0.0

    interim: Any = None
    needs_review = False

    # ── Phase A (+ Phase B if no mismatch) ────────────────────────────────
    with _run_slot(record):
        set_state(record, stage_label="Starting…")
        try:
            interim = _run_phase_a(record, directory_path, inputs)
        except _MismatchSurfaced as ms:
            _stash_mismatch(record, ms.groups, inputs, ms.phase_a_state)
            set_state(record, state="mismatch_pending",
                      progress=STAGE_PROGRESS_PHASE2["awaiting user review"],
                      stage_label=STAGE_LABELS_PHASE2["awaiting user review"])
            needs_review = True
        except (PipelineStopped, Phase2PipelineStopped):
            append_log(record, "Run cancelled by user.")
            set_state(record, state="stopped", stage_label="Stopped")
            return
        except _PipelineErrored as err:
            set_state(record, state="error", error=err.tb, stage_label="Failed",
                      error_title=err.title, error_advice=err.advice,
                      error_category=err.category)
            return
        except Exception as exc:
            _record_unexpected_error(record, exc)
            return

        if not needs_review:
            # Happy path — keep the slot for Phase B.  Avoids handing it
            # to a queued user just to take it back two seconds later.
            try:
                _run_phase_b(record, interim, corrections=[])
            except (PipelineStopped, Phase2PipelineStopped):
                append_log(record, "Run cancelled by user.")
                set_state(record, state="stopped", stage_label="Stopped")
                return
            except _PipelineErrored as err:
                set_state(record, state="error", error=err.tb, stage_label="Failed",
                          error_title=err.title, error_advice=err.advice,
                          error_category=err.category)
                return
            except Exception as exc:
                _record_unexpected_error(record, exc)
                return
            set_state(record, state="done", progress=1.0, stage_label="✓ Complete")
            record_run_duration("phase2", time.time() - phase_a_started_at)
            return

    # ── User review (no slot held — other users can run while we wait) ────
    review_started = time.time()
    record.resume_event.wait()
    review_seconds = time.time() - review_started
    if record.stop_event.is_set():
        append_log(record, "Run cancelled by user.")
        set_state(record, state="stopped", stage_label="Stopped")
        return
    with record.lock:
        corrections = list(record.mismatch_corrections)
        interim = record.phase2_interim or _read_pickle(record.tmpdir / "interim.pkl")
    set_state(record, state="running", stage_label="Resuming with corrections…")

    # ── Phase B (resumed) ─────────────────────────────────────────────────
    with _run_slot(record):
        try:
            _run_phase_b(record, interim, corrections=corrections)
        except (PipelineStopped, Phase2PipelineStopped):
            append_log(record, "Run cancelled by user.")
            set_state(record, state="stopped", stage_label="Stopped")
            return
        except _PipelineErrored as err:
            set_state(record, state="error", error=err.tb, stage_label="Failed",
                      error_title=err.title, error_advice=err.advice,
                      error_category=err.category)
            return
        except Exception as exc:
            _record_unexpected_error(record, exc)
            return
        set_state(record, state="done", progress=1.0, stage_label="✓ Complete")
        record_run_duration("phase2", time.time() - phase_a_started_at - review_seconds)


# ── Phase 2 helpers ───────────────────────────────────────────────────────────

class _MismatchSurfaced(Exception):
    """Internal sentinel — Phase A came back with mismatches to review."""
    def __init__(self, groups: list[dict[str, Any]], phase_a_state: Any) -> None:
        self.groups = groups
        self.phase_a_state = phase_a_state


class _PipelineErrored(Exception):
    """Internal sentinel — the subprocess exited non-zero (≠ 99/42)."""
    def __init__(self, tb: str, title: Optional[str],
                 advice: Optional[str], category: Optional[str]) -> None:
        self.tb = tb
        self.title = title
        self.advice = advice
        self.category = category


def _run_phase_a(record: JobRecord, directory_path: str,
                 inputs: Phase2Inputs) -> Any:
    """Run Phase A in subprocess (or in-process under AIC_INPROCESS)."""
    if _inprocess():
        with _redirect_stdout(record, STAGE_PROGRESS_PHASE2, STAGE_LABELS_PHASE2):
            try:
                return run_phase_a(
                    Path(directory_path), inputs, stop_event=record.stop_event,
                )
            except MismatchReviewNeeded as exc:
                raise _MismatchSurfaced(exc.groups, exc.phase_a_state) from None

    _write_pickle(record.tmpdir / "input.pkl", {
        "directory_path": directory_path,
        "phase2_inputs":  inputs,
    })
    rc = _spawn_pipeline(record, "phase2_a",
                         STAGE_PROGRESS_PHASE2, STAGE_LABELS_PHASE2)
    if rc == 99:
        raise Phase2PipelineStopped()
    if rc == 42:
        raise _MismatchSurfaced(
            _read_pickle(record.tmpdir / "mismatch.pkl"),
            _read_pickle(record.tmpdir / "interim.pkl"),
        )
    if rc != 0:
        raise _PipelineErrored(*_classify_subprocess_error(record))
    return _read_pickle(record.tmpdir / "interim.pkl")


def _run_phase_b(record: JobRecord, interim: Any,
                 corrections: list[dict[str, Any]]) -> None:
    """Run Phase B; output_path is attached to the record on success."""
    if _inprocess():
        with _redirect_stdout(record, STAGE_PROGRESS_PHASE2, STAGE_LABELS_PHASE2):
            result = run_phase_b(
                interim, corrections, output_dir=record.tmpdir,
                stop_event=record.stop_event,
            )
    else:
        _write_pickle(record.tmpdir / "interim.pkl", interim)
        _write_pickle(record.tmpdir / "corrections.pkl", corrections)
        rc = _spawn_pipeline(record, "phase2_b",
                             STAGE_PROGRESS_PHASE2, STAGE_LABELS_PHASE2)
        if rc == 99:
            raise Phase2PipelineStopped()
        if rc != 0:
            raise _PipelineErrored(*_classify_subprocess_error(record))
        result = _read_pickle(record.tmpdir / "result.pkl")

    with record.lock:
        record.output_path = result.output_xlsx_path


def _stash_mismatch(record: JobRecord, groups: list[dict[str, Any]],
                    inputs: Phase2Inputs, phase_a_state: Any) -> None:
    rules = (
        list(inputs.brand_override_config.get("rules", []))
        if isinstance(inputs.brand_override_config, dict) else []
    )
    brand_values, tb_values = collect_dropdown_values(
        groups, main_df=phase_a_state.df,
    )
    with record.lock:
        record.mismatch_groups = serialise_mismatch_groups(
            groups, main_df=phase_a_state.df, brand_override_rules=rules,
        )
        record.mismatch_brand_values      = brand_values
        record.mismatch_tool_brand_values = tb_values
        record.phase2_interim = phase_a_state


def _record_unexpected_error(record: JobRecord, exc: BaseException) -> None:
    tb = traceback.format_exc()
    for ln in tb.splitlines():
        append_log(record, ln)
    friendly = classify(exc)
    set_state(record, state="error", error=tb, stage_label="Failed",
              error_title=friendly.title, error_advice=friendly.advice,
              error_category=friendly.category)


def start_phase2(record: JobRecord, directory_path: str,
                 inputs: Phase2Inputs) -> threading.Thread:
    thread = threading.Thread(
        target=run_phase2_worker,
        args=(record, directory_path, inputs),
        daemon=True,
        name=f"phase2-{record.run_id}",
    )
    thread.start()
    return thread


# ═══════════════════════════════════════════════════════════════════════════
# Post-QC worker (re-upload edited xlsx → category CSVs zip)
# ═══════════════════════════════════════════════════════════════════════════

# Lazily import phase3_package.run_post_qc so the fast tests can monkeypatch
# `worker.run_post_qc` without needing the real ml/phase3 stack on PATH.
try:
    from phase3_package.pipeline import run_post_qc
except ImportError:  # fast-test environments stub phase3 modules
    run_post_qc = None  # type: ignore[assignment]


def run_post_qc_worker(record: JobRecord, edited_xlsx_path: str,
                       is_custom_collapse: bool) -> None:
    import io as _io
    import zipfile as _zipfile

    set_state(record, state="post_qc_running",
              progress=0.05, stage_label="Re-collapsing edited workbook…")

    with _run_slot(record):
        try:
            if _inprocess():
                with _redirect_stdout(record, STAGE_PROGRESS_PHASE2, STAGE_LABELS_PHASE2):
                    if run_post_qc is None:
                        raise RuntimeError("phase3_package.run_post_qc is not available")
                    _, category_splits = run_post_qc(
                        excel_path=edited_xlsx_path,
                        is_custom_collapse=is_custom_collapse,
                    )
            else:
                _write_pickle(record.tmpdir / "input.pkl", {
                    "edited_xlsx_path":  edited_xlsx_path,
                    "is_custom_collapse": is_custom_collapse,
                })
                rc = _spawn_pipeline(record, "post_qc",
                                     STAGE_PROGRESS_PHASE2, STAGE_LABELS_PHASE2)
                if rc != 0:
                    tb, title, advice, cat = _classify_subprocess_error(record)
                    set_state(record, state="error", error=tb,
                              stage_label="Post-QC failed",
                              error_title=title, error_advice=advice, error_category=cat)
                    return
                category_splits = _read_pickle(record.tmpdir / "result.pkl")

            zip_path = record.tmpdir / "post_qc.zip"
            with _zipfile.ZipFile(zip_path, "w", _zipfile.ZIP_DEFLATED) as zf:
                for category, df in category_splits.items():
                    csv_buf = _io.BytesIO()
                    df.to_csv(csv_buf, index=False)
                    zf.writestr(f"{category}.csv", csv_buf.getvalue())
                with record.lock:
                    log_text = "\n".join(record.log_lines)
                zf.writestr("output_QClogs.txt", log_text)

            with record.lock:
                record.post_qc_zip_path = zip_path
                record.post_qc_categories = sorted(category_splits.keys())
            set_state(record, state="post_qc_done", progress=1.0,
                      stage_label="✓ Post-QC export ready")
        except Exception as exc:
            tb = traceback.format_exc()
            for ln in tb.splitlines():
                append_log(record, ln)
            friendly = classify(exc)
            set_state(record, state="error", error=tb,
                      stage_label="Post-QC failed",
                      error_title=friendly.title, error_advice=friendly.advice,
                      error_category=friendly.category)


def start_post_qc(record: JobRecord, edited_xlsx_path: str,
                  is_custom_collapse: bool) -> threading.Thread:
    thread = threading.Thread(
        target=run_post_qc_worker,
        args=(record, edited_xlsx_path, is_custom_collapse),
        daemon=True,
        name=f"post_qc-{record.run_id}",
    )
    thread.start()
    return thread
