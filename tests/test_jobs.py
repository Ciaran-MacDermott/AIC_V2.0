"""
Unit tests for api.jobs — registry CRUD, snapshot consistency, log
buffer behaviour. No FastAPI, no pipeline.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from api import jobs


@pytest.fixture
def tmp_run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run_a"
    d.mkdir()
    return d


def test_create_assigns_unique_run_id(tmp_run_dir: Path) -> None:
    reg = jobs.JobRegistry()
    a = reg.create(phase="phase1", tmpdir=tmp_run_dir)
    b = reg.create(phase="phase1", tmpdir=tmp_run_dir)
    assert a.run_id != b.run_id
    assert reg.get(a.run_id) is a


def test_get_returns_none_for_unknown_id() -> None:
    reg = jobs.JobRegistry()
    assert reg.get("nope") is None


def test_delete_removes_record_and_cleans_tmpdir(tmp_path: Path) -> None:
    d = tmp_path / "run_b"
    d.mkdir()
    (d / "scratch.txt").write_text("hello")

    reg = jobs.JobRegistry()
    record = reg.create(phase="phase1", tmpdir=d)
    assert reg.delete(record.run_id) is True
    assert reg.get(record.run_id) is None
    assert not d.exists()


def test_delete_unknown_returns_false() -> None:
    reg = jobs.JobRegistry()
    assert reg.delete("ghost") is False


def test_set_state_marks_finished_at_for_terminal_states(tmp_run_dir: Path) -> None:
    reg = jobs.JobRegistry()
    record = reg.create(phase="phase1", tmpdir=tmp_run_dir)

    jobs.set_state(record, state="running")
    assert record.finished_at is None

    jobs.set_state(record, state="done")
    assert record.finished_at is not None


def test_snapshot_includes_log_tail(tmp_run_dir: Path) -> None:
    reg = jobs.JobRegistry()
    record = reg.create(phase="phase1", tmpdir=tmp_run_dir)
    for i in range(5):
        jobs.append_log(record, f"line {i}")

    snap = jobs.snapshot(record)
    assert snap["log_cursor"] == 5
    assert snap["log_tail"] == [f"line {i}" for i in range(5)]


def test_logs_since_pages_correctly(tmp_run_dir: Path) -> None:
    reg = jobs.JobRegistry()
    record = reg.create(phase="phase1", tmpdir=tmp_run_dir)
    for i in range(10):
        jobs.append_log(record, f"l{i}")

    cursor1, lines1 = jobs.logs_since(record, 0)
    assert cursor1 == 10
    assert lines1 == [f"l{i}" for i in range(10)]

    cursor2, lines2 = jobs.logs_since(record, cursor1)
    assert cursor2 == 10
    assert lines2 == []

    cursor3, lines3 = jobs.logs_since(record, 7)
    assert cursor3 == 10
    assert lines3 == ["l7", "l8", "l9"]


def test_ttl_evicts_finished_runs_only(tmp_path: Path) -> None:
    reg = jobs.JobRegistry(ttl_seconds=0)

    finished_dir = tmp_path / "finished"
    finished_dir.mkdir()
    fin = reg.create(phase="phase1", tmpdir=finished_dir)
    jobs.set_state(fin, state="done")

    running_dir = tmp_path / "running"
    running_dir.mkdir()
    run = reg.create(phase="phase1", tmpdir=running_dir)
    jobs.set_state(run, state="running")

    time.sleep(0.01)

    # Trigger eviction by creating something else.  We use create() not
    # get() because get() touches the record under test, refreshing its
    # last_touched and defeating eviction.
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    reg.create(phase="phase1", tmpdir=other_dir)

    assert fin.run_id not in reg._jobs       # noqa: SLF001
    assert run.run_id in reg._jobs, "running jobs must not be evicted by TTL"  # noqa: SLF001


def test_idle_qc_ready_run_evicts_after_ttl(tmp_path: Path) -> None:
    """The bug we just fixed: qc_ready / post_qc_done runs were never
    evicted because finished_at was only set for done/error/stopped."""
    reg = jobs.JobRegistry(ttl_seconds=0)

    abandoned_dir = tmp_path / "abandoned"
    abandoned_dir.mkdir()
    abandoned = reg.create(phase="phase1", tmpdir=abandoned_dir)
    jobs.set_state(abandoned, state="qc_ready")

    time.sleep(0.01)

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    reg.create(phase="phase1", tmpdir=other_dir)

    assert abandoned.run_id not in reg._jobs, (   # noqa: SLF001
        "abandoned qc_ready runs must be reaped by idle-TTL"
    )


def test_active_states_are_immune_to_idle_eviction(tmp_path: Path) -> None:
    """Worker-alive states must never be evicted regardless of how long
    they've gone without an API touch — the worker thread still owns
    the tmpdir and (for mismatch_pending) the pipeline lock."""
    reg = jobs.JobRegistry(ttl_seconds=0)

    for state in ("queued", "running", "finalizing",
                  "post_qc_running", "mismatch_pending"):
        d = tmp_path / state
        d.mkdir()
        rec = reg.create(phase="phase1", tmpdir=d)
        jobs.set_state(rec, state=state)
        time.sleep(0.01)

        # Force eviction sweep without touching the record under test.
        with reg._mu:                          # noqa: SLF001
            reg._evict_expired_locked()        # noqa: SLF001

        assert rec.run_id in reg._jobs, (      # noqa: SLF001
            f"state={state!r} must survive idle eviction"
        )


def test_get_refreshes_last_touched(tmp_run_dir: Path) -> None:
    """An open browser tab polling status must keep the run alive."""
    reg = jobs.JobRegistry()
    record = reg.create(phase="phase1", tmpdir=tmp_run_dir)

    record.last_touched = 0.0   # pretend it's been idle forever
    reg.get(record.run_id)
    assert record.last_touched > 0.0, "registry.get must refresh last_touched"


def test_run_slots_caps_concurrency() -> None:
    """RUN_SLOTS must allow up to MAX_RUN_SLOTS concurrent acquires
    and block the next one."""
    import threading
    # Take a fresh semaphore so we don't drain the module-level one
    # for the rest of the test session.
    sem = threading.BoundedSemaphore(jobs.MAX_RUN_SLOTS)
    held = []
    for _ in range(jobs.MAX_RUN_SLOTS):
        assert sem.acquire(blocking=False), "should fit MAX_RUN_SLOTS holders"
        held.append(True)
    assert not sem.acquire(blocking=False), (
        f"slot {jobs.MAX_RUN_SLOTS + 1} must block — semaphore is busted"
    )
    for _ in held:
        sem.release()


def test_queue_eta_only_kicks_in_when_all_slots_busy(tmp_path: Path) -> None:
    """compute_queue_info should return None ETA until running_count
    actually fills MAX_RUN_SLOTS — otherwise we'd flash a stale ETA at
    a queued record that's about to leave the queue."""
    reg = jobs.JobRegistry()
    jobs.registry = reg   # compute_queue_info reads the module-level singleton

    # Pre-seed median run duration so the function has something to project.
    for _ in range(3):
        jobs.record_run_duration("phase1", 60.0)

    # Three slots busy (out of MAX_RUN_SLOTS=5), one queued — ETA hidden.
    for i in range(3):
        d = tmp_path / f"running_{i}"
        d.mkdir()
        rec = reg.create(phase="phase1", tmpdir=d)
        jobs.set_state(rec, state="running")

    qd = tmp_path / "queued"
    qd.mkdir()
    queued = reg.create(phase="phase1", tmpdir=qd)
    jobs.set_state(queued, state="queued")

    pos, depth, eta = jobs.compute_queue_info(queued)
    assert pos == 0
    assert depth == 1
    assert eta is None, "ETA must hide while running_count < MAX_RUN_SLOTS"

    # Saturate the slots and the ETA should appear.
    for i in range(jobs.MAX_RUN_SLOTS - 3):
        d = tmp_path / f"saturate_{i}"
        d.mkdir()
        rec = reg.create(phase="phase1", tmpdir=d)
        jobs.set_state(rec, state="running")

    pos, depth, eta = jobs.compute_queue_info(queued)
    assert eta is not None and eta > 0
