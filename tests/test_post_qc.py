"""
Parity tests for the post-QC re-upload + category-CSV export.

Streamlit's Phase 3 page (lines 1416-1524) lets the analyst:
  1. Download the post-Phase-2 output.xlsx.
  2. Edit the 'Cleaned Output' sheet in Excel.
  3. Re-upload the edited file.
  4. Receive a zip containing per-category CSVs + the QC log.

The refactor needs to expose the same flow so analysts can finish the
Streamlit→FastAPI migration without losing this final step.
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("xlsxwriter")
pytest.importorskip("openpyxl")

from fastapi.testclient import TestClient

from api import jobs
from api.main import app
from tests.fixtures import write_post_qc_xlsx


@pytest.fixture(autouse=True)
def fresh_registry():
    jobs.registry = jobs.JobRegistry()
    yield
    for record in list(jobs.registry._jobs.values()):
        record.stop_event.set()
        record.resume_event.set()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _wait_until(predicate, *, msg: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting: {msg}")


def _seed_done_phase2_run(tmp_path: Path) -> str:
    """Phase 2 run that has completed and is ready for post-QC re-upload."""
    tmpdir = tmp_path / "p2run"
    tmpdir.mkdir()
    record = jobs.registry.create(phase="phase2", tmpdir=tmpdir)
    jobs.set_state(record, state="done", progress=1.0)
    return record.run_id


def test_post_qc_requires_done_phase2_run(client: TestClient, tmp_path: Path) -> None:
    """Re-upload before Phase 2 finishes is a 409 (state mismatch)."""
    tmpdir = tmp_path / "p2run"
    tmpdir.mkdir()
    record = jobs.registry.create(phase="phase2", tmpdir=tmpdir)
    jobs.set_state(record, state="running")

    edited = write_post_qc_xlsx(tmp_path)
    with edited.open("rb") as fh:
        r = client.post(
            f"/api/runs/{record.run_id}/post_qc",
            files={"xlsx": ("output_edited.xlsx", fh,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    assert r.status_code == 409


def test_post_qc_rejects_non_xlsx_extension(client: TestClient, tmp_path: Path) -> None:
    run_id = _seed_done_phase2_run(tmp_path)
    r = client.post(
        f"/api/runs/{run_id}/post_qc",
        files={"xlsx": ("notes.txt", b"x", "text/plain")},
    )
    assert r.status_code == 400


def test_post_qc_runs_re_collapse_and_returns_zip(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """
    The route should kick off the post-QC worker, which calls
    run_post_qc, then bundles the resulting category DataFrames into a
    zip the user can download.  We stub run_post_qc so the test doesn't
    spin up the real Phase 3 quality checks.
    """
    run_id = _seed_done_phase2_run(tmp_path)

    def fake_post_qc(excel_path, is_custom_collapse, meta_df=None):
        category_splits = {
            "AMMO":     pd.DataFrame([{"BRAND": "ACME", "TOOL_BRAND": "ACME"}]),
            "GROCERY":  pd.DataFrame([{"BRAND": "ZETA", "TOOL_BRAND": "ZETA"}]),
        }
        return pd.DataFrame(), category_splits

    from api import worker as worker_mod
    monkeypatch.setattr(worker_mod, "run_post_qc", fake_post_qc)

    edited = write_post_qc_xlsx(tmp_path)
    with edited.open("rb") as fh:
        r = client.post(
            f"/api/runs/{run_id}/post_qc",
            files={"xlsx": ("output_edited.xlsx", fh,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "download_url" in body

    _wait_until(
        lambda: jobs.registry.get(run_id).state == "post_qc_done",
        msg="post-qc worker did not reach post_qc_done",
    )

    # Categories surface on /api/runs/{id} after the worker finishes.
    status = client.get(f"/api/runs/{run_id}").json()
    assert set(status["post_qc_categories"]) == {"AMMO", "GROCERY"}

    r = client.get(body["download_url"])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")

    # Zip contains one CSV per category + a log file.
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "AMMO.csv"     in names
    assert "GROCERY.csv"  in names
    assert any(n.endswith("logs.txt") for n in names)


def test_post_qc_standalone_creates_post_qc_phase_record(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """
    /api/post_qc/standalone creates its own JobRecord with phase='post_qc'
    (not 'phase2') so the dashboard / future ETA bucketing can tell
    standalone re-uploads apart from a full Phase 2 run.  Survives
    parent-run eviction by design — no parent_run_id reference.
    """
    def fake_post_qc(excel_path, is_custom_collapse, meta_df=None):
        return pd.DataFrame(), {
            "AMMO": pd.DataFrame([{"BRAND": "ACME"}]),
        }

    from api import worker as worker_mod
    monkeypatch.setattr(worker_mod, "run_post_qc", fake_post_qc)

    edited = write_post_qc_xlsx(tmp_path)
    with edited.open("rb") as fh:
        r = client.post(
            "/api/post_qc/standalone",
            files={"xlsx": ("output_edited.xlsx", fh,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"is_custom_collapse": "false"},
        )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    record = jobs.registry.get(run_id)
    assert record is not None
    assert record.phase == "post_qc"
    assert record.parent_run_id is None

    _wait_until(
        lambda: jobs.registry.get(run_id).state == "post_qc_done",
        msg="standalone post-qc worker did not reach post_qc_done",
    )


def test_post_qc_propagates_pipeline_errors_into_state(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    run_id = _seed_done_phase2_run(tmp_path)

    def fake_post_qc(*a, **kw):
        raise RuntimeError("simulated quality check failure")

    from api import worker as worker_mod
    monkeypatch.setattr(worker_mod, "run_post_qc", fake_post_qc)

    edited = write_post_qc_xlsx(tmp_path)
    with edited.open("rb") as fh:
        r = client.post(
            f"/api/runs/{run_id}/post_qc",
            files={"xlsx": ("output_edited.xlsx", fh,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    assert r.status_code == 200, r.text

    _wait_until(
        lambda: jobs.registry.get(run_id).state == "error",
        msg="post-qc worker did not reach error state",
    )

    status = client.get(f"/api/runs/{run_id}").json()
    assert status["state"] == "error"
    assert "simulated quality check failure" in (status["error"] or "")
