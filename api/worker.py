"""
Worker thread that executes a Phase 1 pipeline run.

The worker:
  1. Acquires PIPELINE_LOCK so concurrent runs don't trample each other
     via the legacy code's chdir / sys.path mutations.
  2. Redirects sys.stdout/sys.stderr line-by-line into the JobRecord's
     log buffer — same trick the Streamlit app uses, but scoped to the
     lock so it can't interleave with the FastAPI logger.
  3. Calls run_phase1, updating the record's stage/progress as the
     pipeline emits "START …" / "DONE …" lines.
  4. Stores the pipeline payload on the record for the QC layer to use,
     and transitions state to qc_ready (or error/stopped).
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Optional

from api.errors import classify
from api.jobs import (
    JobRecord,
    PIPELINE_LOCK,
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


class _LogStream:
    """
    A line-buffered file-like that funnels print() output into the job's
    log_lines and, while writing, watches for stage transition markers
    so we can update the progress bar without an explicit callback.

    ``progress_table`` and ``label_table`` are the per-phase stage maps
    used for matching — Phase 1 and Phase 2 print different banners so
    each worker passes its own tables in.
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
        # ml_package.write_results uses xlsxwriter, which may probe the
        # real fileno of stdout when running under a TTY. We never want
        # that lookup to succeed against this stream.
        raise OSError("_LogStream has no file descriptor")

    def _emit(self, line: str) -> None:
        append_log(self._record, line)
        self._update_stage(line)

    def _update_stage(self, line: str) -> None:
        low = line.lower()
        is_done = "done" in low
        # Phase 1 banners are 'START …' / 'DONE …' single-keyword lines;
        # Phase 2 banners are full-text section headers (e.g. "PHASE 2
        # AIC PROCESSING").  Matching by substring works for both as
        # long as the keys in each progress table match the way the
        # corresponding pipeline writes them out.
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


def run_phase1_worker(record: JobRecord, excel_path: str, csv_path: str) -> None:
    """Top-level thread target for a Phase 1 run."""
    set_state(record, state="running", stage_label="Waiting for pipeline lock…")

    with PIPELINE_LOCK:
        set_state(record, stage_label="Starting…")
        stream = _LogStream(record, STAGE_PROGRESS, STAGE_LABELS)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = stream
        sys.stderr = stream
        ml_started_at = time.time()
        try:
            payload = _pipeline_mod.run_phase1(
                excel_path, csv_path, stop_event=record.stop_event,
            )
            with record.lock:
                record.pipeline_payload = {
                    "FINAL":         payload.FINAL,
                    "FLAT_FILE_OUT": payload.FLAT_FILE_OUT,
                    "meta":          payload.meta,
                    "dictEnsemble":  payload.dictEnsemble,
                }
            set_state(
                record,
                state="qc_ready",
                progress=STAGE_PROGRESS["qc_ready"],
                stage_label=STAGE_LABELS["qc_ready"],
            )
            # Record only the ML-pipeline duration (excludes interactive
            # QC); that's the part queued runs are actually waiting on.
            record_run_duration("phase1", time.time() - ml_started_at)
        except PipelineStopped:
            append_log(record, "Run cancelled by user.")
            set_state(record, state="stopped", stage_label="Stopped")
        except Exception as exc:
            tb = traceback.format_exc()
            for ln in tb.splitlines():
                append_log(record, ln)
            friendly = classify(exc)
            set_state(
                record, state="error", error=tb, stage_label="Failed",
                error_title=friendly.title,
                error_advice=friendly.advice,
                error_category=friendly.category,
            )
        finally:
            stream.flush()
            sys.stdout, sys.stderr = old_out, old_err


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

def run_phase2_worker(record: JobRecord,
                      directory_path: str,
                      inputs: Phase2Inputs) -> None:
    """
    Top-level thread target for a Phase 2 run.

    Flow:
      1. Acquire PIPELINE_LOCK so the legacy chdir / sys.path mutations
         in ml_package and phase3_package can't trample concurrent runs.
      2. Redirect stdout/stderr into the JobRecord's log buffer.
      3. Run Phase A.  If it surfaces mismatch groups, attach them to
         the record, mark state=mismatch_pending, and park on
         resume_event until the analyst posts corrections.  The route
         layer is responsible for setting the event when corrections
         arrive (or when a stop signal does).
      4. Run Phase B.  On success, the cleaned-output xlsx path is
         attached to the record and state moves to 'done'.
    """
    from pathlib import Path

    set_state(record, state="running", stage_label="Waiting for pipeline lock…")

    with PIPELINE_LOCK:
        set_state(record, stage_label="Starting…")
        stream = _LogStream(record, STAGE_PROGRESS_PHASE2, STAGE_LABELS_PHASE2)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = stream
        sys.stderr = stream
        # Don't include time spent parked on resume_event waiting for the
        # analyst — that's not pipeline-bound time.
        phase_a_started_at = time.time()
        review_seconds: float = 0.0
        try:
            # ── Phase A ─────────────────────────────────────────────
            try:
                interim = run_phase_a(
                    Path(directory_path), inputs, stop_event=record.stop_event,
                )
            except MismatchReviewNeeded as exc:
                # Attach the JSON-safe groups + the in-memory state
                # needed to resume Phase B, then park. Enrich with
                # DESCRIPTION/RMRR/_is_expected so the React grid can
                # render the same shape the Streamlit page did.
                rules = list(
                    inputs.brand_override_config.get("rules", [])
                ) if isinstance(inputs.brand_override_config, dict) else []
                brand_values, tb_values = collect_dropdown_values(
                    exc.groups, main_df=exc.phase_a_state.df,
                )
                with record.lock:
                    record.mismatch_groups = serialise_mismatch_groups(
                        exc.groups,
                        main_df=exc.phase_a_state.df,
                        brand_override_rules=rules,
                    )
                    record.mismatch_brand_values      = brand_values
                    record.mismatch_tool_brand_values = tb_values
                    record.phase2_interim = exc.phase_a_state
                set_state(
                    record,
                    state="mismatch_pending",
                    progress=STAGE_PROGRESS_PHASE2["awaiting user review"],
                    stage_label=STAGE_LABELS_PHASE2["awaiting user review"],
                )
                # Wait for either a stop or a resolve. The route layer
                # writes corrections onto record.mismatch_corrections
                # before setting resume_event.
                review_started = time.time()
                record.resume_event.wait()
                review_seconds = time.time() - review_started
                if record.stop_event.is_set():
                    raise Phase2PipelineStopped()

                with record.lock:
                    interim = record.phase2_interim
                    corrections = list(record.mismatch_corrections)
                set_state(record, state="running",
                          stage_label="Resuming with corrections…")
            else:
                corrections = []

            # ── Phase B ─────────────────────────────────────────────
            result = run_phase_b(
                interim, corrections, output_dir=record.tmpdir,
                stop_event=record.stop_event,
            )
            with record.lock:
                record.output_path = result.output_xlsx_path
            set_state(
                record, state="done", progress=1.0, stage_label="✓ Complete",
            )
            record_run_duration(
                "phase2", time.time() - phase_a_started_at - review_seconds,
            )

        except (Phase2PipelineStopped, PipelineStopped):
            append_log(record, "Run cancelled by user.")
            set_state(record, state="stopped", stage_label="Stopped")
        except Exception as exc:
            tb = traceback.format_exc()
            for ln in tb.splitlines():
                append_log(record, ln)
            friendly = classify(exc)
            set_state(
                record, state="error", error=tb, stage_label="Failed",
                error_title=friendly.title,
                error_advice=friendly.advice,
                error_category=friendly.category,
            )
        finally:
            stream.flush()
            sys.stdout, sys.stderr = old_out, old_err


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
    """
    Run the post-QC pipeline against ``edited_xlsx_path`` and bundle
    the resulting per-category CSVs + log into a zip on disk.

    Uses STAGE_LABELS_PHASE2 progress tables for log streaming so the
    log box behaves the same as Phase A/B.
    """
    import io as _io
    import zipfile as _zipfile

    set_state(record, state="post_qc_running",
              progress=0.05, stage_label="Re-collapsing edited workbook…")

    with PIPELINE_LOCK:
        stream = _LogStream(record, STAGE_PROGRESS_PHASE2, STAGE_LABELS_PHASE2)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = stream
        sys.stderr = stream
        try:
            if run_post_qc is None:
                raise RuntimeError("phase3_package.run_post_qc is not available")
            _, category_splits = run_post_qc(
                excel_path=edited_xlsx_path,
                is_custom_collapse=is_custom_collapse,
            )

            zip_path = record.tmpdir / "post_qc.zip"
            with _zipfile.ZipFile(zip_path, "w", _zipfile.ZIP_DEFLATED) as zf:
                for category, df in category_splits.items():
                    csv_buf = _io.BytesIO()
                    df.to_csv(csv_buf, index=False)
                    zf.writestr(f"{category}.csv", csv_buf.getvalue())
                # Bundle the running log so analysts have full context
                # alongside the exports — matches output_QClogs.txt in the
                # Streamlit version.
                with record.lock:
                    log_text = "\n".join(record.log_lines)
                zf.writestr("output_QClogs.txt", log_text)

            with record.lock:
                record.post_qc_zip_path = zip_path
                record.post_qc_categories = sorted(category_splits.keys())

            set_state(
                record, state="post_qc_done", progress=1.0,
                stage_label="✓ Post-QC export ready",
            )
        except Exception as exc:
            tb = traceback.format_exc()
            for ln in tb.splitlines():
                append_log(record, ln)
            friendly = classify(exc)
            set_state(
                record, state="error", error=tb,
                stage_label="Post-QC failed",
                error_title=friendly.title,
                error_advice=friendly.advice,
                error_category=friendly.category,
            )
        finally:
            stream.flush()
            sys.stdout, sys.stderr = old_out, old_err


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
