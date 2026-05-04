"""
Phase 2 + 3 end-to-end integration test.

The cleanest, lowest-fragility way to drive real phase3_package end to
end is to chain off a real Phase 1 run: we upload the synthetic Phase 1
fixture, finalize it through the QC layer to get a genuine
``File_For_Mapping_QC.xlsx`` (with the real lookup sheets phase3 reads),
then bundle that workbook with the three txt files into a Phase 2 zip
and drive Phase 2 through the BFF.

This keeps fixture maintenance to one place — the existing Phase 1
fixture — and makes the test resilient to changes in the lookup-sheet
schema (any drift would already be flagged by Phase 1's e2e test).

Skipped automatically if heavy phase3 deps aren't installed.
"""

from __future__ import annotations

import io
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
    write_phase1_inputs,
)


POLL_TIMEOUT = 120.0
POLL_INTERVAL = 0.25


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


def _wait_for(client: TestClient, run_id: str, target_states: set[str]) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/runs/{run_id}").json()
        if last["state"] in target_states:
            return last
        if last["state"] == "error":
            raise AssertionError(
                f"Pipeline errored: {last.get('error')}\n"
                + "\n".join(last.get("log_tail") or [])
            )
        time.sleep(POLL_INTERVAL)
    raise AssertionError(
        f"Timed out waiting for {target_states}; last state={last.get('state')!r}"
    )


def _run_real_phase1_and_finalize(client: TestClient, tmp_path: Path) -> Path:
    """
    Drive a real Phase 1 run, skip QC, finalize, and return the path of
    the produced File_For_Mapping_QC.xlsx so Phase 2 can consume it.
    """
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

    r = client.post(f"/api/runs/{run_id}/qc/finalize")
    assert r.status_code == 200, r.text

    # Pull the workbook out of the run's tmpdir.
    record = jobs.registry.get(run_id)
    qc_xlsx_path = record.output_path
    assert qc_xlsx_path.exists(), qc_xlsx_path
    return qc_xlsx_path


def _build_phase2_zip(qc_xlsx: Path, dest_dir: Path) -> Path:
    """Bundle the real QC workbook with the three Phase 2 txt files."""
    dest_dir.mkdir(exist_ok=True)
    zip_path = dest_dir / "phase2.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(qc_xlsx, arcname="File_For_Mapping_QC.xlsx")
        zf.writestr("ModelInfo.txt",      _model_info_txt())
        zf.writestr("Attributes.txt",     _attributes_txt())
        zf.writestr("AttributeValues.txt", _attribute_values_txt())
    return zip_path


def _config_form() -> dict[str, str]:
    return {
        "config": (
            '{"raw_upc_pl_brand_col":"RAW_BRAND",'
            '"is_custom_collapse":false,'
            '"skip_rmrr":true}'
        ),
    }


def test_phase2_chained_off_real_phase1(client: TestClient, tmp_path: Path) -> None:
    qc_xlsx = _run_real_phase1_and_finalize(client, tmp_path)
    zip_path = _build_phase2_zip(qc_xlsx, tmp_path / "p2")

    # ── Upload Phase 2 zip ─────────────────────────────────────────────
    with zip_path.open("rb") as fh:
        r = client.post(
            "/api/phase2/runs",
            files={"zip": ("phase2.zip", fh, "application/zip")},
            data=_config_form(),
        )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    # ── Phase A → done OR mismatch_pending ─────────────────────────────
    status = _wait_for(client, run_id, {"mismatch_pending", "done"})
    if status["state"] == "mismatch_pending":
        # Take the no-changes path — accept all originals so Phase B runs.
        r = client.post(
            f"/api/runs/{run_id}/mismatch/resolve",
            json={"corrections": []},
        )
        assert r.status_code == 200, r.text
        status = _wait_for(client, run_id, {"done"})

    assert status["state"] == "done"
    assert status["progress"] == 1.0

    # ── Download workbook + sanity-check shape ─────────────────────────
    r = client.get(f"/api/runs/{run_id}/artifacts/output.xlsx")
    assert r.status_code == 200
    assert zipfile.is_zipfile(io.BytesIO(r.content))

    # Cleaned Output sheet must be present.
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True)
    assert "Cleaned Output" in wb.sheetnames
    wb.close()
