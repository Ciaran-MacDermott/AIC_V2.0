"""
End-to-end FastAPI tests using TestClient.

We bypass the real Phase 1 pipeline by injecting a fake pipeline_payload
straight onto the JobRecord — exercises every QC route + finalize +
download without needing pandas/numpy/xgboost. The pipeline itself is
covered by a separate slow test (out of scope for this fast suite).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

from fastapi.testclient import TestClient

from api import jobs
from api.main import app


@pytest.fixture(autouse=True)
def fresh_registry():
    """Each test gets a clean registry so run_ids don't leak between tests."""
    jobs.registry = jobs.JobRegistry()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_oversize_upload_rejected_by_middleware(client: TestClient) -> None:
    """Content-Length over the cap must be rejected with 413 + a friendly
    payload, before the route handler ever reads the body."""
    headers = {"content-length": str(10 * 1024 * 1024 * 1024)}  # 10 GB
    r = client.post(
        "/api/phase1/runs",
        files={"xlsx": ("a.xlsx", b"x", "application/octet-stream"),
               "csv":  ("a.csv",  b"x", "text/csv")},
        headers=headers,
    )
    assert r.status_code == 413, r.text
    body = r.json()
    assert body["error_category"] == "input"
    assert "MB" in body["error_advice"]


def _seed_qc_ready_run(tmp_path: Path) -> str:
    """Create a registry record that looks like a finished pipeline."""
    tmpdir = tmp_path / "seeded"
    tmpdir.mkdir()
    record = jobs.registry.create(phase="phase1", tmpdir=tmpdir)

    df = pd.DataFrame([
        {"BRAND": "ACME", "MLBRAND": "ACME", "score": 100,
         "QC Priority": "LOW",  "ML Matches Lookup": "Yes", "Note": ""},
        {"BRAND": "?",    "MLBRAND": "ZETA", "score": 60,
         "QC Priority": "HIGH", "ML Matches Lookup": "No",  "Note": "check"},
    ])
    record.pipeline_payload = {
        "FINAL":         pd.DataFrame(),
        "FLAT_FILE_OUT": pd.DataFrame(),
        "meta":          pd.DataFrame(),
        "dictEnsemble":  {"Final_BRAND_lkp": df},
    }
    jobs.set_state(record, state="qc_ready", progress=0.85, stage_label="QC ready")
    return record.run_id


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_get_run_404_for_unknown(client: TestClient) -> None:
    r = client.get("/api/runs/does-not-exist")
    assert r.status_code == 404


def test_status_snapshot_round_trips(client: TestClient, tmp_path: Path) -> None:
    run_id = _seed_qc_ready_run(tmp_path)

    r = client.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert body["state"] == "qc_ready"
    assert body["qc_sheet_keys"] == ["Final_BRAND_lkp"]


def test_qc_sheets_lists_summary(client: TestClient, tmp_path: Path) -> None:
    run_id = _seed_qc_ready_run(tmp_path)

    r = client.get(f"/api/runs/{run_id}/qc/sheets")
    assert r.status_code == 200
    sheets = r.json()["sheets"]
    assert sheets == [{
        "key": "Final_BRAND_lkp",
        "label": "BRAND",
        "row_count": 2,
        "edited_count": 0,
    }]


def test_qc_sheet_payload_returns_rows_and_flags(client: TestClient, tmp_path: Path) -> None:
    run_id = _seed_qc_ready_run(tmp_path)

    r = client.get(f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp")
    assert r.status_code == 200
    body = r.json()
    assert body["attribute"] == "BRAND"
    # High-priority row sorts to the top.
    assert "high_priority" in body["row_flags"][body["rows"][0]["_row_id"]]
    assert "BRAND" in [c["field"] for c in body["columns"] if c["editable"]]


def test_qc_save_persists_edits_and_summary_reflects_them(client: TestClient, tmp_path: Path) -> None:
    run_id = _seed_qc_ready_run(tmp_path)

    r = client.put(
        f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp",
        json={"edited_rows": [{"row_id": "r0", "attribute_value": "ZETA"}]},
    )
    assert r.status_code == 204

    r = client.get(f"/api/runs/{run_id}/qc/sheets")
    assert r.json()["sheets"][0]["edited_count"] == 1


def test_qc_save_404_for_unknown_sheet(client: TestClient, tmp_path: Path) -> None:
    run_id = _seed_qc_ready_run(tmp_path)
    r = client.put(
        f"/api/runs/{run_id}/qc/sheets/does_not_exist",
        json={"edited_rows": []},
    )
    assert r.status_code == 404


def test_finalize_writes_xlsx_and_marks_done(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """
    write_results is a real (vendored) function but writing an xlsx needs
    xlsxwriter. Patch the module-level reference to a stub that just
    drops a marker file so the route logic is exercised end-to-end.
    """
    run_id = _seed_qc_ready_run(tmp_path)

    written: list[Path] = []

    def fake_write(out_path, *args, **kwargs):
        Path(out_path).write_bytes(b"fake-xlsx")
        written.append(Path(out_path))

    from api import pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "write_results", fake_write)

    r = client.post(f"/api/runs/{run_id}/qc/finalize")
    assert r.status_code == 200
    body = r.json()
    assert body["download_url"] == f"/api/runs/{run_id}/artifacts/qc.xlsx"
    assert len(written) == 1

    r = client.get(f"/api/runs/{run_id}")
    assert r.json()["state"] == "done"
    assert r.json()["progress"] == 1.0

    r = client.get(body["download_url"])
    assert r.status_code == 200
    assert r.content == b"fake-xlsx"


def test_stop_sets_event(client: TestClient, tmp_path: Path) -> None:
    run_id = _seed_qc_ready_run(tmp_path)
    r = client.post(f"/api/runs/{run_id}/stop")
    assert r.status_code == 204
    assert jobs.registry.get(run_id).stop_event.is_set()


def test_delete_removes_run(client: TestClient, tmp_path: Path) -> None:
    run_id = _seed_qc_ready_run(tmp_path)
    r = client.delete(f"/api/runs/{run_id}")
    assert r.status_code == 204

    r = client.get(f"/api/runs/{run_id}")
    assert r.status_code == 404


def test_create_phase1_run_validates_extensions(client: TestClient) -> None:
    r = client.post(
        "/api/phase1/runs",
        files={
            "xlsx": ("notes.txt", b"x", "text/plain"),
            "csv":  ("data.csv", b"x", "text/csv"),
        },
    )
    assert r.status_code == 400
