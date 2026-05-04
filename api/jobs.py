"""
Job registry for long-running pipeline runs.

Each run gets a JobRecord that the worker thread mutates as it makes
progress.  The HTTP layer reads the record's snapshot via getters, so
multiple concurrent polls don't see torn state.

Concurrency: every pipeline stage runs in a child process spawned by
``api.worker._spawn_pipeline``, isolated from ml_package's global-state
mutations.  RUN_SLOTS (a BoundedSemaphore) caps how many can be in
flight at once; runs beyond that sit in state="queued" until a slot
opens, with queue_position + ETA surfaced via /api/runs/{id}.

Eviction: idle runs (state ∉ _ACTIVE_STATES) are reaped after
ttl_seconds of inactivity by both inline registry calls and a
background reaper thread.  Worker progress and any API touch refresh
the record's last_touched timestamp.
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


# Concurrent-run cap.  Each pipeline subprocess takes roughly 200–400 MB
# resident plus a few seconds of cold-start; on a 4-vCPU box, 5 in flight
# is comfortable while keeping plenty of headroom for FastAPI itself.
# Beyond MAX_RUN_SLOTS, runs queue at state="queued" and the ETA logic
# in compute_queue_info() kicks in.
MAX_RUN_SLOTS = 5
RUN_SLOTS = threading.BoundedSemaphore(MAX_RUN_SLOTS)

# Soft cap on how many recent log lines we hand back in a status snapshot.
LOG_TAIL_SIZE = 60

# States where the worker thread is actively executing or parked on a
# bounded wait — records here are immune to idle-TTL eviction.
# `mismatch_pending` is intentionally absent: the Phase 2 worker now
# parks with a 2h timeout, so a stale mismatch_pending record means
# either the user abandoned it or the worker has already exited.
_ACTIVE_STATES = frozenset({
    "queued", "running", "finalizing", "post_qc_running",
})

_RUNNING_STATES = frozenset({
    "running", "finalizing", "post_qc_running",
})

_TERMINAL_STATES = frozenset({"done", "error", "stopped"})

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

    def evict_expired(self) -> None:
        """Public entry point for the background reaper."""
        with self._mu:
            self._evict_expired_locked()

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
            if state in _TERMINAL_STATES:
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

    With MAX_RUN_SLOTS slots, the first MAX_RUN_SLOTS runs go straight to
    running.  Anyone behind that sits at state="queued"; this function
    tells the UI roughly how long they'll wait.  Returns (None,…) for
    already-running records so the UI hides the queue chip.
    """
    if record.state != "queued":
        return None, None, None

    active = registry.list_active()
    queued_sorted = sorted(
        [r for r in active if r.state == "queued"],
        key=lambda r: r.started_at,
    )
    running_count = sum(1 for r in active if r.state in _RUNNING_STATES)

    try:
        position_in_queue = queued_sorted.index(record)
    except ValueError:
        position_in_queue = 0

    # 0 = next in line for a slot.  Depth is the queue length only; the
    # currently-running runs aren't a "queue" anymore — they've all got
    # their own slot.
    queue_position = position_in_queue
    queue_depth    = len(queued_sorted)

    # ETA estimate: with MAX_RUN_SLOTS independent runs going, on average
    # one finishes every (median / MAX_RUN_SLOTS) seconds.  (position+1)
    # finishes need to happen before we get a slot.  Only project when
    # all slots are full — otherwise being "queued" is a transient state
    # the worker is about to leave.
    median = median_run_duration(record.phase)
    if median and running_count >= MAX_RUN_SLOTS:
        eta_seconds = (position_in_queue + 1) * median / MAX_RUN_SLOTS
    else:
        eta_seconds = None
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
            registry.evict_expired()
        except Exception:
            # The reaper must never die — swallow and try again.
            pass


threading.Thread(target=_reap_loop, name="job-reaper", daemon=True).start()
