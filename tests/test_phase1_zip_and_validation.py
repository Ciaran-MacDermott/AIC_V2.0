"""
Parity tests for the Phase 1 input layer.

The Streamlit prototype accepts EITHER a zip (containing the Excel
+ csv) or two loose files; it also surfaces three explicit RuntimeError
strings on malformed Excel input.  These tests cover both code paths
end-to-end against the FastAPI BFF — fast suite, no real ML deps used.

Heavy ML modules are stubbed via tests/conftest.py so the worker is
swapped for a pipeline that just emits the validation errors verbatim
(see ``_seed_pipeline_for_validation`` below).  The shape of the
errors and the upload routes are what we're checking, not the ML
output itself — that's covered by tests/integration/test_phase1_real.py.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("xlsxwriter")
pytest.importorskip("openpyxl")

from fastapi.testclient import TestClient

from api import jobs
from api.main import app
from tests.fixtures import (
    write_malformed_phase1_xlsx,
    write_phase1_inputs,
    write_phase1_zip,
)


@pytest.fixture(autouse=True)
def fresh_registry():
    jobs.registry = jobs.JobRegistry()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _wait_for_state(client: TestClient, run_id: str, *,
                    states: set[str], timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/runs/{run_id}").json()
        if last["state"] in states:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"Timed out waiting for {states}; last state={last.get('state')!r}, "
        f"log_tail={last.get('log_tail')}"
    )


# ── ZIP upload mode ──────────────────────────────────────────────────────────

def test_phase1_zip_upload_creates_run(client: TestClient, tmp_path: Path,
                                       monkeypatch) -> None:
    """A bare zip with xlsx+csv at the root kicks off a Phase 1 run."""
    # Patch the pipeline so we don't run the real ML stack — we only want
    # to verify the route accepted the zip and started the worker.
    from api import pipeline as pipeline_mod
    from api.pipeline import Phase1Payload

    captured: dict = {}

    def fake_pipeline(excel_path, csv_path, stop_event=None):
        captured["excel"] = excel_path
        captured["csv"]   = csv_path
        return Phase1Payload(
            FINAL=pd.DataFrame(),
            FLAT_FILE_OUT=pd.DataFrame(),
            meta=pd.DataFrame(),
            dictEnsemble={},
        )
    monkeypatch.setattr(pipeline_mod, "run_phase1", fake_pipeline)

    zip_path = write_phase1_zip(tmp_path)
    with zip_path.open("rb") as fh:
        r = client.post(
            "/api/phase1/runs/zip",
            files={"zip": ("fixture.zip", fh, "application/zip")},
        )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    # Worker has resolved the xlsx + csv inside the extracted zip.
    _wait_for_state(client, run_id, states={"qc_ready", "error", "stopped"})
    assert captured["excel"].endswith("fixture.xlsx")
    assert captured["csv"].endswith("fixture.csv")


def test_phase1_zip_unwraps_single_top_level_folder(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """Streamlit unwraps a single wrapper directory inside the zip."""
    from api import pipeline as pipeline_mod
    from api.pipeline import Phase1Payload

    seen: dict = {}

    def fake_pipeline(excel_path, csv_path, stop_event=None):
        seen["excel"] = excel_path
        seen["csv"]   = csv_path
        return Phase1Payload(
            FINAL=pd.DataFrame(), FLAT_FILE_OUT=pd.DataFrame(),
            meta=pd.DataFrame(), dictEnsemble={},
        )
    monkeypatch.setattr(pipeline_mod, "run_phase1", fake_pipeline)

    zip_path = write_phase1_zip(tmp_path, with_wrapper_folder=True)
    with zip_path.open("rb") as fh:
        r = client.post(
            "/api/phase1/runs/zip",
            files={"zip": ("wrapped.zip", fh, "application/zip")},
        )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    _wait_for_state(client, run_id, states={"qc_ready", "error", "stopped"})
    assert "/project/" in seen["excel"], seen["excel"]


def test_phase1_zip_missing_xlsx_returns_400(
    client: TestClient, tmp_path: Path
) -> None:
    """No Excel with META+FINAL → upfront 400, never starts a job."""
    zip_path = write_phase1_zip(tmp_path, omit_xlsx=True)
    with zip_path.open("rb") as fh:
        r = client.post(
            "/api/phase1/runs/zip",
            files={"zip": ("bad.zip", fh, "application/zip")},
        )
    assert r.status_code == 400
    assert "Excel" in r.json()["detail"] or "META" in r.json()["detail"]


def test_phase1_zip_missing_csv_returns_400(
    client: TestClient, tmp_path: Path
) -> None:
    zip_path = write_phase1_zip(tmp_path, omit_csv=True)
    with zip_path.open("rb") as fh:
        r = client.post(
            "/api/phase1/runs/zip",
            files={"zip": ("nocsv.zip", fh, "application/zip")},
        )
    assert r.status_code == 400
    assert "csv" in r.json()["detail"].lower()


def test_phase1_zip_with_wrong_extension_returns_400(
    client: TestClient, tmp_path: Path
) -> None:
    fake = tmp_path / "fake.tar"
    fake.write_bytes(b"not-a-zip")
    with fake.open("rb") as fh:
        r = client.post(
            "/api/phase1/runs/zip",
            files={"zip": ("fake.tar", fh, "application/x-tar")},
        )
    assert r.status_code == 400


def test_phase1_zip_with_corrupt_bytes_returns_400(
    client: TestClient, tmp_path: Path
) -> None:
    """Filename ends in .zip but contents aren't a valid archive."""
    bad = tmp_path / "looks_like.zip"
    bad.write_bytes(b"not-actually-a-zip")
    with bad.open("rb") as fh:
        r = client.post(
            "/api/phase1/runs/zip",
            files={"zip": ("looks_like.zip", fh, "application/zip")},
        )
    assert r.status_code == 400


# ── Excel content validation (mid-pipeline RuntimeError parity) ──────────────
# These bypass the route layer (no ML deps installed in the fast suite) and
# call run_phase1 directly so we can assert on the exact RuntimeError text
# the Streamlit page raises in 1_Phase_1_Attribute_Mapping.py:354-377.

def test_validation_missing_meta_sheet_raises(tmp_path: Path, monkeypatch) -> None:
    from api import pipeline as pipeline_mod

    # Skip past the Lookup/BM25/XGBoost/ensemble call sites — the
    # validation code we want to exercise runs *before* any of them.
    monkeypatch.setattr(pipeline_mod._ml,  "runLookup",    lambda *a, **k: ({}, None, None, {}))
    monkeypatch.setattr(pipeline_mod._tm,  "runTextMatch", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_mod._rfx, "runML",        lambda *a, **k: {})
    monkeypatch.setattr(pipeline_mod._ens, "runEnsemble",  lambda *a, **k: {})

    bad_xlsx = write_malformed_phase1_xlsx(
        tmp_path, drop_meta=True, name="bad.xlsx",
    )
    _, csv_path = write_phase1_inputs(tmp_path)

    with pytest.raises(RuntimeError, match="No META sheet"):
        pipeline_mod.run_phase1(str(bad_xlsx), str(csv_path))


def test_validation_missing_final_sheet_raises(tmp_path: Path, monkeypatch) -> None:
    from api import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod._ml,  "runLookup",    lambda *a, **k: ({}, None, None, {}))
    monkeypatch.setattr(pipeline_mod._tm,  "runTextMatch", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_mod._rfx, "runML",        lambda *a, **k: {})
    monkeypatch.setattr(pipeline_mod._ens, "runEnsemble",  lambda *a, **k: {})

    bad_xlsx = write_malformed_phase1_xlsx(
        tmp_path, drop_final=True, name="bad.xlsx",
    )
    _, csv_path = write_phase1_inputs(tmp_path)

    with pytest.raises(RuntimeError, match="No FINAL sheet"):
        pipeline_mod.run_phase1(str(bad_xlsx), str(csv_path))


def test_validation_missing_meta_column_raises(tmp_path: Path, monkeypatch) -> None:
    from api import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod._ml,  "runLookup",    lambda *a, **k: ({}, None, None, {}))
    monkeypatch.setattr(pipeline_mod._tm,  "runTextMatch", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_mod._rfx, "runML",        lambda *a, **k: {})
    monkeypatch.setattr(pipeline_mod._ens, "runEnsemble",  lambda *a, **k: {})

    bad_xlsx = write_malformed_phase1_xlsx(
        tmp_path, drop_meta_column="Attribute Name in MDM", name="bad.xlsx",
    )
    _, csv_path = write_phase1_inputs(tmp_path)

    with pytest.raises(RuntimeError, match="META sheet missing column"):
        pipeline_mod.run_phase1(str(bad_xlsx), str(csv_path))


def test_validation_failure_via_worker_marks_state_error(
    client: TestClient, tmp_path: Path
) -> None:
    """
    A malformed xlsx posted via the loose-file route should reach the
    worker, which catches the RuntimeError and surfaces it as state=error
    on the JobRecord — matching the Streamlit 'stage=failed' behaviour.
    """
    bad_xlsx = write_malformed_phase1_xlsx(tmp_path, drop_meta=True, name="bad.xlsx")
    _, good_csv = write_phase1_inputs(tmp_path)

    with bad_xlsx.open("rb") as xfh, good_csv.open("rb") as cfh:
        r = client.post(
            "/api/phase1/runs",
            files={
                "xlsx": ("fixture.xlsx", xfh,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                "csv":  ("fixture.csv",  cfh, "text/csv"),
            },
        )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    final = _wait_for_state(client, run_id, states={"error", "qc_ready"})
    assert final["state"] == "error"
    assert "No META sheet" in (final["error"] or "") or any(
        "No META sheet" in ln for ln in final["log_tail"]
    )
