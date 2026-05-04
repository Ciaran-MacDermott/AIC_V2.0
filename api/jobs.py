"""
Job registry for long-running pipeline runs.

Each run gets a JobRecord that the worker thread mutates as it makes
progress. The HTTP layer reads the record's snapshot via getters, so
multiple concurrent polls don't see torn state.

Process-wide pipeline lock: the legacy ml_package code mutates module
globals (sys.path) and the parent Streamlit app uses os.chdir. To stay
safe across worker threads we serialise pipeline execution with a single
Lock. Only one run executes the heavy stages at a time; queued runs sit
in `queued` state until the lock is free.

A future refactor can drop this lock by running each pipeline in a
multiprocessing.Process instead — out of scope for this pass.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# Held by a worker for the duration of one pipeline execution.
PIPELINE_LOCK = threading.Lock()

# Soft cap on how many recent log lines we hand back in a status snapshot.
LOG_TAIL_SIZE = 60

# Rolling window of recent run durations (seconds), per phase.  Used to
# project an ETA for queued runs in the UI — much more useful than a
# blank "Waiting for pipeline lock…" placeholder when 5 users hit the
# server at once.  Bounded so old runs don't skew the median.
_RECENT_DURATIONS: dict[str, deque[float]] = {
    "phase1": deque(maxlen=20),
    "phase2": deque(maxlen=20),
}
_DURATIONS_LOCK = threading.Lock()


def record_run_duration(phase: str, seconds: float) -> None:
    if phase not in _RECENT_DURATIONS:
        return
    with _DURATIONS_LOCK:
        _RECENT_DURATIONS[phase].append(seconds)


def median_run_duration(phase: str) -> Optional[float]:
    """Median of the last ~20 successful runs, or None if we have no data yet."""
    with _DURATIONS_LOCK:
        d = list(_RECENT_DURATIONS.get(phase, []))
    if not d:
        return None
    d.sort()
    mid = len(d) // 2
    return d[mid] if len(d) % 2 else (d[mid - 1] + d[mid]) / 2


@dataclass
class JobRecord:
    run_id: str
    phase: str                          # "phase1" | "phase2"
    tmpdir: Path
    parent_run_id: Optional[str] = None

    # Mutable run state — guarded by `lock`.
    state: str = "queued"
    progress: float = 0.0
    stage_label: str = "Queued"
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    # Last sign of life — refreshed by both worker progress (set_state)
    # and any API touch (registry.get).  Eviction is keyed on this so
    # runs whose tab is closed get reaped, but a long QC session that
    # keeps fetching sheets stays alive.
    last_touched: float = field(default_factory=time.time)
    error: Optional[str] = None
    # User-facing error metadata.  ``error`` keeps the technical
    # traceback for support; these two power the dialog the UI renders
    # so the analyst sees a remediation, not a stack trace.
    error_title:    Optional[str] = None
    error_advice:   Optional[str] = None
    error_category: Optional[str] = None    # "input" | "config" | "server"

    # Pipeline outputs (populated on success).
    pipeline_payload: Optional[dict[str, Any]] = None     # FINAL/FLAT/META/dictEnsemble (Phase 1)
    output_path: Optional[Path] = None                    # finalised xlsx

    # Post-QC re-upload outputs (Phase 2/3 only).
    post_qc_zip_path: Optional[Path] = None
    post_qc_categories: list[str] = field(default_factory=list)

    # Phase 2 mismatch-review state.
    # Held on the record across the resume_event.wait() so the route
    # layer can serve the mismatch payload and apply corrections.
    mismatch_groups: list[dict[str, Any]] = field(default_factory=list)
    mismatch_brand_values: list[str] = field(default_factory=list)
    mismatch_tool_brand_values: list[str] = field(default_factory=list)
    mismatch_corrections: list[dict[str, Any]] = field(default_factory=list)
    phase2_interim: Optional[Any] = None                  # Phase2InterimState

    # QC editor state.
    qc_edits: dict[str, dict[str, str]] = field(default_factory=dict)
    # row_id -> attribute_value, per sheet key

    # Coordination primitives.
    stop_event: threading.Event = field(default_factory=threading.Event)
    resume_event: threading.Event = field(default_factory=threading.Event)
    log_lines: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


# States where a worker thread is actively executing or parked.  Records
# in these states are NEVER evicted by TTL — only an explicit DELETE or
# the worker reaching a non-active state can release them.  Note that
# `mismatch_pending` is included here even though it's user-driven: the
# Phase 2 worker is parked on resume_event holding PIPELINE_LOCK, so
# evicting the record without signalling stop_event would leak the
# thread.  Stop-on-timeout for stuck mismatch reviews is a separate
# follow-up.
_ACTIVE_STATES = frozenset({
    "queued", "running", "finalizing", "post_qc_running", "mismatch_pending",
})


class JobRegistry:
    """In-memory store of JobRecords with idle-TTL eviction."""

    def __init__(self, ttl_seconds: int = 60 * 60):
        self._jobs: dict[str, JobRecord] = {}
        self._mu = threading.Lock()
        self._ttl = ttl_seconds

    def create(self, phase: str, tmpdir: Path, parent_run_id: Optional[str] = None) -> JobRecord:
        with self._mu:
            self._evict_expired_locked()
            run_id = uuid.uuid4().hex[:12]
            record = JobRecord(
                run_id=run_id, phase=phase, tmpdir=tmpdir, parent_run_id=parent_run_id,
            )
            self._jobs[run_id] = record
            return record

    def get(self, run_id: str) -> Optional[JobRecord]:
        with self._mu:
            self._evict_expired_locked()
            record = self._jobs.get(run_id)
        if record is not None:
            # Touch outside the registry lock to keep contention low —
            # record.lock is per-record.
            with record.lock:
                record.last_touched = time.time()
        return record

    def delete(self, run_id: str) -> bool:
        with self._mu:
            record = self._jobs.pop(run_id, None)
        if record is None:
            return False
        _cleanup_tmpdir(record.tmpdir)
        return True

    def list_active(self) -> list["JobRecord"]:
        """Snapshot of all live records (any state) for the dashboard endpoint."""
        with self._mu:
            self._evict_expired_locked()
            return list(self._jobs.values())

    # Caller must hold self._mu.
    def _evict_expired_locked(self) -> None:
        now = time.time()
        expired = [
            rid for rid, r in self._jobs.items()
            if r.state not in _ACTIVE_STATES
            and (now - r.last_touched) > self._ttl
        ]
        for rid in expired:
            record = self._jobs.pop(rid)
            _cleanup_tmpdir(record.tmpdir)


def _cleanup_tmpdir(path: Path) -> None:
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# ── Mutators used by the worker thread ───────────────────────────────────────

def append_log(record: JobRecord, line: str) -> None:
    with record.lock:
        record.log_lines.append(line)


def set_state(record: JobRecord, *, state: Optional[str] = None,
              progress: Optional[float] = None,
              stage_label: Optional[str] = None,
              error: Optional[str] = None,
              error_title: Optional[str] = None,
              error_advice: Optional[str] = None,
              error_category: Optional[str] = None) -> None:
    with record.lock:
        # Worker progress counts as activity for the idle-TTL clock too.
        record.last_touched = time.time()
        if state is not None:
            record.state = state
            if state in ("done", "error", "stopped"):
                record.finished_at = time.time()
        if progress is not None:
            record.progress = progress
        if stage_label is not None:
            record.stage_label = stage_label
        if error is not None:
            record.error = error
        if error_title is not None:
            record.error_title = error_title
        if error_advice is not None:
            record.error_advice = error_advice
        if error_category is not None:
            record.error_category = error_category


def snapshot(record: JobRecord) -> dict[str, Any]:
    """Read a consistent snapshot for a status response."""
    queue_position, queue_depth, eta_seconds = compute_queue_info(record)
    with record.lock:
        return {
            "run_id":            record.run_id,
            "phase":             record.phase,
            "state":             record.state,
            "progress":          record.progress,
            "stage_label":       record.stage_label,
            "started_at":        record.started_at,
            "finished_at":       record.finished_at,
            "error":             record.error,
            "error_title":       record.error_title,
            "error_advice":      record.error_advice,
            "error_category":    record.error_category,
            "parent_run_id":     record.parent_run_id,
            "log_cursor":        len(record.log_lines),
            "log_tail":          list(record.log_lines[-LOG_TAIL_SIZE:]),
            "qc_sheet_keys":     (
                list(record.pipeline_payload["dictEnsemble"].keys())
                if record.pipeline_payload else None
            ),
            "mismatch_count":    len(record.mismatch_groups) or None,
            "post_qc_categories": (
                list(record.post_qc_categories) if record.post_qc_categories else None
            ),
            "queue_position":    queue_position,
            "queue_depth":       queue_depth,
            "eta_seconds":       eta_seconds,
        }


def compute_queue_info(record: JobRecord) -> tuple[Optional[int], Optional[int], Optional[float]]:
    """
    Project (queue_position, queue_depth, eta_seconds) for ``record``.

    Mirrors what users would see if they had visibility into PIPELINE_LOCK:
    the worker on the lock is ahead of them, plus any other queued workers.
    Only meaningful while record.state == 'queued'; for already-running
    records we return (None, None, None) so the UI hides the queue chip.
    """
    if record.state != "queued":
        return None, None, None

    # Snapshot all active records, then sort queued ones by created order.
    active = registry.list_active()
    queued_sorted = sorted(
        [r for r in active if r.state == "queued"],
        key=lambda r: r.started_at,
    )
    running_count = sum(
        1 for r in active if r.state in ("running", "finalizing", "post_qc_running")
    )

    try:
        position_in_queue = queued_sorted.index(record)
    except ValueError:
        position_in_queue = 0

    # Queue position counts the records ahead of you, including the one
    # actively holding PIPELINE_LOCK (if any).
    queue_position = running_count + position_in_queue
    queue_depth    = running_count + len(queued_sorted)

    median = median_run_duration(record.phase)
    eta_seconds = (median * (queue_position + 1)) if median else None
    return queue_position, queue_depth, eta_seconds


def logs_since(record: JobRecord, since: int) -> tuple[int, list[str]]:
    with record.lock:
        n = len(record.log_lines)
        if since < 0 or since >= n:
            return n, []
        return n, list(record.log_lines[since:])


# Singleton — the FastAPI app imports `registry` directly.
registry = JobRegistry()


def _reap_loop() -> None:
    """Background reaper — runs eviction even when the API is idle.

    Without this, eviction only fires when a request comes in.  An
    abandoned `qc_ready` run with no incoming traffic would survive
    indefinitely.  Tick once a minute; eviction itself is cheap.
    """
    while True:
        time.sleep(60)
        try:
            with registry._mu:                    # noqa: SLF001
                registry._evict_expired_locked()  # noqa: SLF001
        except Exception:
            # The reaper must never die — swallow and try again.
            pass


threading.Thread(target=_reap_loop, name="job-reaper", daemon=True).start()
