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

    # Trigger eviction by calling get() on something else.
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    reg.create(phase="phase1", tmpdir=other_dir)

    assert reg.get(fin.run_id) is None
    assert reg.get(run.run_id) is run, "running jobs must not be evicted by TTL"
