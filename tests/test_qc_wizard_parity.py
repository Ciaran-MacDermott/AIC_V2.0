"""
Parity tests for the QC wizard's interactive layers.

Streamlit's per-attribute QC sheet has these behaviours that must
survive the refactor:

  * Sort order: HIGH → MEDIUM → LOW priority, No → Yes ML agreement,
    score ascending.  (Drives the "fix the worst rows first" UX.)
  * ML<attr> column renamed to 'ML Suggestion' for display, restored
    on write so write_results sees the legacy shape.
  * Dropdown options = union(historical attribute values, ML suggestions),
    deduped, with empty-string first so analysts can clear a cell.
  * Per-row flags: high_priority, low_score_no_ml, has_note — these
    drive the AG Grid cellClassRules tinting in the React grid.
  * Edits round-trip: PUT-then-GET shows the edit, finalize writes the
    edited dataframe back into write_results.
  * Skip semantics: an unedited sheet writes the original DataFrame.
  * Stop semantics: stop during pipeline → state=stopped, no QC payload.

Most of these are unit-tested in test_qc_view.py.  This module covers
the *route-level* wiring + the few invariants that span multiple
components (skip, finalize round-trip, stop).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

from fastapi.testclient import TestClient

from api import jobs
from api.main import app


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


def _seed_qc_run(tmp_path: Path) -> tuple[str, jobs.JobRecord]:
    tmpdir = tmp_path / "run"
    tmpdir.mkdir()
    record = jobs.registry.create(phase="phase1", tmpdir=tmpdir)
    df = pd.DataFrame([
        {"BRAND": "?",     "MLBRAND": "ZETA", "score": 50,
         "QC Priority": "HIGH",   "ML Matches Lookup": "No",  "Note": "double"},
        {"BRAND": "ACME",  "MLBRAND": "ACME", "score": 80,
         "QC Priority": "MEDIUM", "ML Matches Lookup": "No",  "Note": ""},
        {"BRAND": "ACME",  "MLBRAND": "ACME", "score": 100,
         "QC Priority": "LOW",    "ML Matches Lookup": "Yes", "Note": ""},
    ])
    import pickle as _pickle
    heavy_path = tmpdir / "phase1_heavy.pkl"
    with open(heavy_path, "wb") as f:
        _pickle.dump({
            "FINAL":         pd.DataFrame(),
            "FLAT_FILE_OUT": pd.DataFrame(),
            "meta":          pd.DataFrame(),
        }, f)
    record.pipeline_payload = {
        "dictEnsemble": {"Final_BRAND_lkp": df},
        "_heavy_path":  str(heavy_path),
    }
    jobs.set_state(record, state="qc_ready")
    return record.run_id, record


# ── Sort + display layer ───────────────────────────────────────────────────

def test_qc_payload_sorts_by_priority_then_ml_then_score(
    client: TestClient, tmp_path: Path
) -> None:
    run_id, _ = _seed_qc_run(tmp_path)
    payload = client.get(f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp").json()
    rows = payload["rows"]

    # HIGH/no/50 sorts before MEDIUM/no/80 sorts before LOW/yes/100.
    priorities = [r["QC Priority"] for r in rows]
    assert priorities == ["HIGH", "MEDIUM", "LOW"]


def test_qc_payload_renames_ml_column_to_suggestion(
    client: TestClient, tmp_path: Path
) -> None:
    run_id, _ = _seed_qc_run(tmp_path)
    payload = client.get(f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp").json()
    fields = [c["field"] for c in payload["columns"]]
    assert "ML Suggestion" in fields
    assert "MLBRAND" not in fields


def test_qc_payload_flags_high_priority_low_score_and_notes(
    client: TestClient, tmp_path: Path
) -> None:
    run_id, _ = _seed_qc_run(tmp_path)
    payload = client.get(f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp").json()

    high_id = payload["rows"][0]["_row_id"]
    flags = payload["row_flags"][high_id]
    assert "high_priority" in flags
    assert "low_score_no_ml" in flags
    assert "has_note" in flags


def test_qc_payload_dropdown_includes_blank_first(
    client: TestClient, tmp_path: Path
) -> None:
    run_id, _ = _seed_qc_run(tmp_path)
    payload = client.get(f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp").json()
    options = payload["attribute_options"]
    assert options[0] == ""           # clear-value sentinel comes first
    assert "ACME" in options
    assert "ZETA" in options          # ML suggestion bleeds in


# ── Edit round-trip ───────────────────────────────────────────────────────

def test_edit_then_get_echoes_back_on_same_row(
    client: TestClient, tmp_path: Path
) -> None:
    run_id, _ = _seed_qc_run(tmp_path)

    sheet = client.get(f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp").json()
    rid = sheet["rows"][0]["_row_id"]   # HIGH row

    r = client.put(
        f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp",
        json={"edited_rows": [{"row_id": rid, "attribute_value": "ZETA_FINAL"}]},
    )
    assert r.status_code == 204

    sheet = client.get(f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp").json()
    edited_row = next(r for r in sheet["rows"] if r["_row_id"] == rid)
    assert edited_row["BRAND"] == "ZETA_FINAL"
    # Original is preserved so the React grid can colour the edited cell.
    assert sheet["original_values"][rid] == "?"


def test_edit_count_surfaces_in_summary(client: TestClient, tmp_path: Path) -> None:
    run_id, _ = _seed_qc_run(tmp_path)

    sheet = client.get(f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp").json()
    rid = sheet["rows"][1]["_row_id"]
    client.put(
        f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp",
        json={"edited_rows": [{"row_id": rid, "attribute_value": "OMEGA"}]},
    )

    summary = client.get(f"/api/runs/{run_id}/qc/sheets").json()
    by_key = {s["key"]: s for s in summary["sheets"]}
    assert by_key["Final_BRAND_lkp"]["edited_count"] == 1


# ── Finalize: edits flow into write_results, skipped sheets pass through ───

def test_finalize_with_edits_writes_edited_dataframe(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    run_id, _ = _seed_qc_run(tmp_path)

    sheet = client.get(f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp").json()
    rid = sheet["rows"][0]["_row_id"]
    client.put(
        f"/api/runs/{run_id}/qc/sheets/Final_BRAND_lkp",
        json={"edited_rows": [{"row_id": rid, "attribute_value": "ZETA_FINAL"}]},
    )

    received: list[dict] = []

    def fake_write(out_path, FINAL, FLAT_FILE_OUT, meta, dict_ensemble):
        Path(out_path).write_bytes(b"fake-xlsx")
        received.append(dict_ensemble)

    from api import pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "write_results", fake_write)

    r = client.post(f"/api/runs/{run_id}/qc/finalize")
    assert r.status_code == 200, r.text

    assert len(received) == 1
    df_written = received[0]["Final_BRAND_lkp"]
    # The HIGH-priority row sorts to index 0 — its BRAND value reflects the edit.
    assert df_written.iloc[0]["BRAND"] == "ZETA_FINAL"
    # ML Suggestion has been renamed back to MLBRAND for write_results.
    assert "MLBRAND" in df_written.columns
    assert "ML Suggestion" not in df_written.columns


def test_finalize_with_no_edits_passes_original_dataframe(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """Skip flow: never PUT an edit; finalize hands the original df to write_results."""
    run_id, record = _seed_qc_run(tmp_path)
    original_df = record.pipeline_payload["dictEnsemble"]["Final_BRAND_lkp"]

    received: list[dict] = []
    def fake_write(out_path, FINAL, FLAT_FILE_OUT, meta, dict_ensemble):
        Path(out_path).write_bytes(b"fake-xlsx")
        received.append(dict_ensemble)
    from api import pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "write_results", fake_write)

    r = client.post(f"/api/runs/{run_id}/qc/finalize")
    assert r.status_code == 200

    df_written = received[0]["Final_BRAND_lkp"]
    # write_results saw the *exact* original frame — same identity.
    assert df_written is original_df


# ── Stop semantics during QC ───────────────────────────────────────────────

def test_stop_during_qc_marks_stop_event(client: TestClient, tmp_path: Path) -> None:
    """
    Stop during the QC stage doesn't unwind a worker (worker is done by
    qc_ready), but it MUST set the stop_event so any background flushes
    or post-QC threads observe it.
    """
    run_id, record = _seed_qc_run(tmp_path)
    assert not record.stop_event.is_set()

    r = client.post(f"/api/runs/{run_id}/stop")
    assert r.status_code == 204
    assert jobs.registry.get(run_id).stop_event.is_set()
    # And the resume_event so any parked Phase 2 worker also wakes.
    assert jobs.registry.get(run_id).resume_event.is_set()


def test_qc_save_404s_for_unknown_sheet(client: TestClient, tmp_path: Path) -> None:
    run_id, _ = _seed_qc_run(tmp_path)
    r = client.put(
        f"/api/runs/{run_id}/qc/sheets/Final_NOPE_lkp",
        json={"edited_rows": []},
    )
    assert r.status_code == 404


def test_qc_save_409s_when_pipeline_not_ready(
    client: TestClient, tmp_path: Path
) -> None:
    """A run that's still running has no pipeline_payload — saves must 409."""
    tmpdir = tmp_path / "running"; tmpdir.mkdir()
    record = jobs.registry.create(phase="phase1", tmpdir=tmpdir)
    jobs.set_state(record, state="running")

    r = client.put(
        f"/api/runs/{record.run_id}/qc/sheets/Final_BRAND_lkp",
        json={"edited_rows": []},
    )
    assert r.status_code == 409
