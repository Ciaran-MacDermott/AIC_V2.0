"""
Parity tests for Phase 2 input modes.

Streamlit's Phase 3 page accepts:
  1. ZIP upload  (current refactor: implemented)
  2. Loose-files upload — File_For_Mapping_QC.xlsx + 3 .txt files
  3. Phase-1 handoff via parent_run_id (re-uses Phase 1's tmpdir)

This module covers (2) and (3), plus the column-autodetect helper that
Streamlit exposes via two selectboxes (RAW_UPC priority + RAW_MFR
priority) so the user doesn't have to type column names by hand.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("xlsxwriter")
pytest.importorskip("openpyxl")

from fastapi.testclient import TestClient

from api import jobs
from api.main import app
from api.pipeline_phase2 import Phase2InterimState, Phase2Result
from tests.fixtures import (
    write_phase2_loose_files,
    write_phase2_qc_xlsx,
    write_phase2_zip,
)


WAIT_TIMEOUT = 5.0


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


def _wait_until(predicate, *, msg: str) -> None:
    deadline = time.time() + WAIT_TIMEOUT
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting: {msg}")


def _config_json() -> str:
    return json.dumps({
        "raw_upc_pl_brand_col": "RAW_BRAND",
        "is_custom_collapse":   False,
        "skip_rmrr":            False,
    })


def _stub_phase2_workers(monkeypatch) -> dict:
    """Stub run_phase_a/b so we can exercise the route layer in isolation."""
    seen: dict = {}

    def fake_a(directory_path, inputs, stop_event=None):
        seen["directory_path"] = str(directory_path)
        seen["raw_upc"]        = inputs.raw_upc_pl_brand_col
        return Phase2InterimState(
            df=pd.DataFrame(), duplicate_dimkeys=pd.DataFrame(), pipeline_context={},
        )

    def fake_b(state, corrections, output_dir, stop_event=None):
        out = output_dir / "output.xlsx"
        out.write_bytes(b"OK")
        return Phase2Result(
            collapsed_df=state.df,
            duplicate_dimkeys=state.duplicate_dimkeys,
            output_xlsx_path=out,
        )

    from api import worker as worker_mod
    monkeypatch.setattr(worker_mod, "run_phase_a", fake_a)
    monkeypatch.setattr(worker_mod, "run_phase_b", fake_b)
    return seen


# ── Loose-files mode ────────────────────────────────────────────────────────

def test_phase2_loose_files_upload_creates_run(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    seen = _stub_phase2_workers(monkeypatch)
    files = write_phase2_loose_files(tmp_path)

    with files["xlsx"].open("rb") as xfh, \
         files["model_info"].open("rb") as mfh, \
         files["attributes"].open("rb") as afh, \
         files["attribute_values"].open("rb") as avfh:
        r = client.post(
            "/api/phase2/runs/files",
            files={
                "xlsx":             ("File_For_Mapping_QC.xlsx", xfh,
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                "model_info":       ("ModelInfo.txt",       mfh,  "text/plain"),
                "attributes":       ("Attributes.txt",      afh,  "text/plain"),
                "attribute_values": ("AttributeValues.txt", avfh, "text/plain"),
            },
            data={"config": _config_json()},
        )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    _wait_until(
        lambda: jobs.registry.get(run_id).state == "done",
        msg="loose-files Phase 2 did not reach done",
    )
    # Worker received a directory containing all four files.
    d = Path(seen["directory_path"])
    assert (d / "File_For_Mapping_QC.xlsx").exists()
    assert (d / "ModelInfo.txt").exists()
    assert (d / "Attributes.txt").exists()
    assert (d / "AttributeValues.txt").exists()


def test_phase2_loose_files_rejects_wrong_xlsx_extension(client: TestClient) -> None:
    r = client.post(
        "/api/phase2/runs/files",
        files={
            "xlsx":             ("notes.txt", b"x", "text/plain"),
            "model_info":       ("ModelInfo.txt",       b"x", "text/plain"),
            "attributes":       ("Attributes.txt",      b"x", "text/plain"),
            "attribute_values": ("AttributeValues.txt", b"x", "text/plain"),
        },
        data={"config": _config_json()},
    )
    assert r.status_code == 400


# ── Column autodetect ──────────────────────────────────────────────────────

def test_phase2_scan_returns_default_upc_and_mfr_columns(
    client: TestClient, tmp_path: Path
) -> None:
    """
    The Streamlit page selects defaults from a priority list when the
    user uploads inputs.  The /scan endpoint should return the same
    autodetected defaults so the new UI can pre-populate its dropdowns.
    """
    zip_path = write_phase2_zip(tmp_path)
    with zip_path.open("rb") as fh:
        r = client.post(
            "/api/phase2/scan",
            files={"zip": ("p2.zip", fh, "application/zip")},
        )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["raw_upc_columns"], body
    assert body["raw_manufacturer_columns"], body
    # Defaults match the priority lists in pages/2_Phase_3_Pipeline_and_QC.py.
    assert body["default_upc_col"]            in body["raw_upc_columns"]
    assert body["default_manufacturer_col"]   in body["raw_manufacturer_columns"]
    # Brand + tool-brand value lists power the mismatch dropdowns.
    assert "ACME" in body["brand_values"]
    assert "ACME" in body["tool_brand_values"]
    # scan_id is kept in the response model for API stability but the
    # tmpdir is now reaped before the response — nothing consumes the
    # token, so it's intentionally empty.
    assert body["scan_id"] == ""


def test_phase2_scan_handles_loose_files(client: TestClient, tmp_path: Path) -> None:
    files = write_phase2_loose_files(tmp_path)
    with files["xlsx"].open("rb") as xfh:
        r = client.post(
            "/api/phase2/scan/xlsx",
            files={"xlsx": ("File_For_Mapping_QC.xlsx", xfh,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "RAW_BRAND" in body["raw_upc_columns"]
    assert "RAW_MANUFACTURER" in body["raw_manufacturer_columns"]


def test_phase2_scan_handles_xlsx_with_no_flat_file_sheet(
    client: TestClient, tmp_path: Path
) -> None:
    """If the workbook is missing FLAT_FILE we should 400 with a clear message."""
    bad = tmp_path / "bad.xlsx"
    with pd.ExcelWriter(bad, engine="xlsxwriter") as w:
        pd.DataFrame({"x": [1]}).to_excel(w, sheet_name="OTHER", index=False)

    with bad.open("rb") as fh:
        r = client.post(
            "/api/phase2/scan/xlsx",
            files={"xlsx": ("bad.xlsx", fh,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    assert r.status_code == 400
    assert "flat_file" in r.json()["detail"].lower()


# ── Parent-run handoff (Phase 1 → Phase 2) ─────────────────────────────────

def test_phase2_run_from_phase1_parent(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """
    A Phase 1 run that uploaded a zip with txt files should be reusable
    as the input directory for a Phase 2 run via parent_run_id.
    """
    seen = _stub_phase2_workers(monkeypatch)

    # Seed a Phase 1 record whose tmpdir already contains the four files.
    parent_dir = tmp_path / "phase1_run"
    parent_dir.mkdir()
    write_phase2_loose_files(parent_dir)
    parent = jobs.registry.create(phase="phase1", tmpdir=parent_dir)
    jobs.set_state(parent, state="done")
    parent.output_path = parent_dir / "File_For_Mapping_QC.xlsx"

    r = client.post(
        f"/api/phase2/runs/from-parent/{parent.run_id}",
        data={"config": _config_json()},
    )
    assert r.status_code == 200, r.text
    child_run_id = r.json()["run_id"]
    record = jobs.registry.get(child_run_id)
    assert record.parent_run_id == parent.run_id

    _wait_until(
        lambda: jobs.registry.get(child_run_id).state == "done",
        msg="from-parent Phase 2 run did not reach done",
    )
    # The worker saw a directory with all four files copied across.
    d = Path(seen["directory_path"])
    assert (d / "File_For_Mapping_QC.xlsx").exists()
    assert (d / "ModelInfo.txt").exists()


def test_phase2_run_from_parent_404_when_parent_unknown(client: TestClient) -> None:
    r = client.post(
        "/api/phase2/runs/from-parent/does-not-exist",
        data={"config": _config_json()},
    )
    assert r.status_code == 404
