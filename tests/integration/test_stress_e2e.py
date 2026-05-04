"""
End-to-end stress test: 2 happy paths + 2 failure paths.

The pass scenarios drive the real ml_package / phase3_package end to end
and confirm that:
  - the run produces the expected terminal state and primary artifact,
  - the new bundle.zip route packages every artifact + decision the
    analyst made on this run.

The fail scenarios upload deliberately-broken inputs and confirm that:
  - the run reaches state == "error",
  - the worker classifies the failure into a user-facing dialog payload
    (error_title + error_advice + error_category) rather than dumping a
    raw traceback,
  - the technical detail is still preserved on `error` for support.

Skipped automatically if the heavy ML stack isn't installed (see
tests/integration/conftest.py).
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import jobs
from api.main import app
from tests.fixtures import (
    _attribute_values_txt,
    _attributes_txt,
    _model_info_txt,
    write_malformed_phase1_xlsx,
    write_phase1_inputs,
    write_phase2_zip,
)


POLL_TIMEOUT  = 180.0
POLL_INTERVAL = 0.25


@pytest.fixture(autouse=True)
def fresh_registry():
    jobs.registry = jobs.JobRegistry()
    yield
    # Drain any worker still parked on resume_event before the registry
    # is replaced — otherwise daemon threads outlive the test and the
    # next test's PIPELINE_LOCK acquisition can wedge.
    for record in list(jobs.registry._jobs.values()):
        record.stop_event.set()
        record.resume_event.set()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _wait_for(client: TestClient, run_id: str, target_states: set[str],
              tolerate_error: bool = False) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/runs/{run_id}").json()
        if last["state"] in target_states:
            return last
        if last["state"] == "error" and not tolerate_error:
            raise AssertionError(
                f"Pipeline errored: {last.get('error')}\n"
                + "\n".join(last.get("log_tail") or [])
            )
        time.sleep(POLL_INTERVAL)
    raise AssertionError(
        f"Timed out waiting for {target_states}; last state={last.get('state')!r}"
    )


# ── helpers shared with test_phase2_real ────────────────────────────────────

def _run_real_phase1_and_finalize(client: TestClient, tmp_path: Path) -> tuple[str, Path]:
    p1_dir = tmp_path / "p1"
    p1_dir.mkdir(exist_ok=True)
    xlsx_path, csv_path = write_phase1_inputs(p1_dir)
    with xlsx_path.open("rb") as xfh, csv_path.open("rb") as cfh:
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
    _wait_for(client, run_id, {"qc_ready"})

    # Make a representative QC edit so the bundle has something to serialise.
    sheets = client.get(f"/api/runs/{run_id}/qc/sheets").json()["sheets"]
    brand_key = next(s["key"] for s in sheets if s["label"] == "BRAND")
    payload = client.get(f"/api/runs/{run_id}/qc/sheets/{brand_key}").json()
    first = payload["rows"][0]
    new_val = "ZETA" if first["BRAND"] != "ZETA" else "ACME"
    client.put(
        f"/api/runs/{run_id}/qc/sheets/{brand_key}",
        json={"edited_rows": [{"row_id": first["_row_id"], "attribute_value": new_val}]},
    )

    r = client.post(f"/api/runs/{run_id}/qc/finalize")
    assert r.status_code == 200, r.text
    record = jobs.registry.get(run_id)
    return run_id, record.output_path


def _build_phase2_zip_from_qc(qc_xlsx: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(exist_ok=True)
    zip_path = dest_dir / "phase2.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(qc_xlsx, arcname="File_For_Mapping_QC.xlsx")
        zf.writestr("ModelInfo.txt",       _model_info_txt())
        zf.writestr("Attributes.txt",      _attributes_txt())
        zf.writestr("AttributeValues.txt", _attribute_values_txt())
    return zip_path


def _phase2_config_form() -> dict[str, str]:
    return {
        "config": (
            '{"raw_upc_pl_brand_col":"RAW_BRAND",'
            '"is_custom_collapse":false,'
            '"skip_rmrr":true}'
        ),
    }


# ── PASS A: Phase 1 happy path + bundle ─────────────────────────────────────

def test_pass_phase1_happy_path_and_bundle(
    client: TestClient, tmp_path: Path,
) -> None:
    run_id, output_path = _run_real_phase1_and_finalize(client, tmp_path)
    assert output_path.exists()

    final = client.get(f"/api/runs/{run_id}").json()
    assert final["state"] == "done"
    assert final["progress"] == 1.0

    # Bundle: must be a real zip containing the qc workbook, the log,
    # the QC edits we made, and a metadata header.
    r = client.get(f"/api/runs/{run_id}/artifacts/bundle.zip")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    bundle = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(bundle.namelist())
    assert "qc.xlsx" in names
    assert "log.txt" in names
    assert "qc_edits.json" in names
    assert "metadata.json" in names

    meta = json.loads(bundle.read("metadata.json"))
    assert meta["run_id"] == run_id
    assert meta["phase"]  == "phase1"
    assert meta["state"]  == "done"

    # The qc.xlsx in the bundle is the same artifact the qc.xlsx route serves.
    direct = client.get(f"/api/runs/{run_id}/artifacts/qc.xlsx").content
    assert bundle.read("qc.xlsx") == direct


# ── PASS B: Phase 2 happy path chained off Phase 1 + bundle ─────────────────

def test_pass_phase2_chained_happy_path_and_bundle(
    client: TestClient, tmp_path: Path,
) -> None:
    _, qc_xlsx = _run_real_phase1_and_finalize(client, tmp_path)
    zip_path = _build_phase2_zip_from_qc(qc_xlsx, tmp_path / "p2")

    with zip_path.open("rb") as fh:
        r = client.post(
            "/api/phase2/runs",
            files={"zip": ("phase2.zip", fh, "application/zip")},
            data=_phase2_config_form(),
        )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    status = _wait_for(client, run_id, {"mismatch_pending", "done"})
    if status["state"] == "mismatch_pending":
        client.post(f"/api/runs/{run_id}/mismatch/resolve",
                    json={"corrections": []})
        status = _wait_for(client, run_id, {"done"})
    assert status["state"] == "done"

    r = client.get(f"/api/runs/{run_id}/artifacts/bundle.zip")
    assert r.status_code == 200
    bundle = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(bundle.namelist())
    assert "output.xlsx" in names
    assert "log.txt"     in names
    assert "metadata.json" in names

    meta = json.loads(bundle.read("metadata.json"))
    assert meta["run_id"] == run_id
    assert meta["phase"]  == "phase2"
    assert meta["state"]  == "done"


# ── FAIL A: Phase 1 missing FINAL sheet → friendly dialog ───────────────────

def test_fail_phase1_missing_final_sheet_surfaces_friendly_dialog(
    client: TestClient, tmp_path: Path,
) -> None:
    bad_xlsx = write_malformed_phase1_xlsx(tmp_path, drop_final=True)
    # Any csv is fine — the run never gets past the META/FINAL guard.
    csv_path = tmp_path / "fixture.csv"
    csv_path.write_text("UPC,DESCRIPTION\n123,Acme thing\n")

    with bad_xlsx.open("rb") as xfh, csv_path.open("rb") as cfh:
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

    status = _wait_for(client, run_id, {"error"}, tolerate_error=True)
    assert status["state"] == "error"

    # The friendly-error fields are the load-bearing UX contract: if
    # they're populated, the React dialog renders a remediation; if not,
    # the user sees a raw traceback.
    assert status["error_title"]    == "Missing required sheet"
    advice = status["error_advice"] or ""
    assert "META" in advice and "FINAL" in advice
    assert status["error_category"] == "input"
    # Technical detail still kept around for support escalation.
    assert "FINAL" in (status["error"] or "")


# ── FAIL B: Phase 2 zip missing the QC workbook → friendly dialog ───────────

def test_fail_phase2_missing_qc_xlsx_surfaces_friendly_dialog(
    client: TestClient, tmp_path: Path,
) -> None:
    """
    Analyst forgets to include File_For_Mapping_QC.xlsx in the project zip.
    The pipeline can't proceed — we expect a remediation dialog telling
    them which file is missing, not a raw stack trace.
    """
    p2_dir = tmp_path / "p2_no_qc"
    p2_dir.mkdir()
    zip_path = p2_dir / "phase2.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # File_For_Mapping_QC.xlsx deliberately omitted.
        zf.writestr("ModelInfo.txt",       _model_info_txt())
        zf.writestr("Attributes.txt",      _attributes_txt())
        zf.writestr("AttributeValues.txt", _attribute_values_txt())

    with zip_path.open("rb") as fh:
        r = client.post(
            "/api/phase2/runs",
            files={"zip": ("phase2.zip", fh, "application/zip")},
            data=_phase2_config_form(),
        )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    status = _wait_for(client, run_id, {"error"}, tolerate_error=True)
    assert status["state"] == "error"

    # The contract: error_title + advice are populated (so the dialog
    # renders) and aren't just the raw exception.  We don't pin to exact
    # text because the exact exception type phase3 raises (FileNotFoundError
    # vs ValueError vs AttributeError) varies with library versions.
    assert status["error_title"], (
        f"no friendly title — analyst would see a raw traceback. "
        f"raw error:\n{status.get('error')}"
    )
    assert status["error_advice"]
    assert status["error_category"] in ("input", "config", "server")
    assert status["error"]   # technical detail preserved for support
