"""
Phase 1 end-to-end integration test.

Drives the real FastAPI BFF + real ml_package pipeline against a synthetic
xlsx + csv fixture, exercising every stage:

   upload  →  background pipeline (lookup → BM25 → XGB → ensemble)
            →  qc_ready  →  GET sheet payload  →  PUT an edit
            →  finalize  →  download xlsx artifact

This is the strongest possible automated check that the refactor still
produces correct output: if it passes, every layer of the BFF is wired
into ml_package correctly.

Slow — takes ~30s on a quiet laptop. Skipped automatically if heavy ML
deps aren't installed (see conftest.py).
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
from tests.fixtures import write_phase1_inputs


POLL_INTERVAL = 0.25
POLL_TIMEOUT  = 120.0   # generous — XGBoost cold-start dominates


@pytest.fixture(autouse=True)
def fresh_registry():
    jobs.registry = jobs.JobRegistry()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _wait_for(client: TestClient, run_id: str, target_states: set[str]) -> dict:
    """Poll /api/runs/{id} until state ∈ target_states (or timeout)."""
    deadline = time.time() + POLL_TIMEOUT
    last: dict = {}
    while time.time() < deadline:
        r = client.get(f"/api/runs/{run_id}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last["state"] in target_states:
            return last
        if last["state"] == "error":
            raise AssertionError(f"Pipeline errored: {last.get('error')}\n{last.get('log_tail')}")
        time.sleep(POLL_INTERVAL)
    raise AssertionError(f"Timed out waiting for {target_states}; last state={last.get('state')}")


def test_phase1_end_to_end(client: TestClient, tmp_path: Path) -> None:
    xlsx_path, csv_path = write_phase1_inputs(tmp_path)

    # ── 1. Upload ──────────────────────────────────────────────────────
    with xlsx_path.open("rb") as xfh, csv_path.open("rb") as cfh:
        r = client.post(
            "/api/phase1/runs",
            files={
                "xlsx": ("fixture.xlsx", xfh, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                "csv":  ("fixture.csv",  cfh, "text/csv"),
            },
        )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    # ── 2. Wait for pipeline to finish all four stages ─────────────────
    status = _wait_for(client, run_id, {"qc_ready"})
    assert status["progress"] >= 0.85
    assert status["qc_sheet_keys"], (
        f"ensemble produced no lookup sheets. log_tail:\n  "
        + "\n  ".join(status.get("log_tail", []))
    )

    # ── 3. List QC sheets and confirm both attributes are present ─────
    r = client.get(f"/api/runs/{run_id}/qc/sheets")
    assert r.status_code == 200
    sheets = r.json()["sheets"]
    labels = {s["label"] for s in sheets}
    assert labels == {"BRAND", "PACK_SIZE"}, f"got {labels}"

    # ── 4. Fetch BOTH sheets and confirm distinct dropdowns per table ──
    brand_key = next(s["key"] for s in sheets if s["label"] == "BRAND")
    pack_key  = next(s["key"] for s in sheets if s["label"] == "PACK_SIZE")

    r = client.get(f"/api/runs/{run_id}/qc/sheets/{brand_key}")
    assert r.status_code == 200
    brand_payload = r.json()
    assert brand_payload["attribute"] == "BRAND"
    assert len(brand_payload["rows"]) > 0
    editable_fields = [c["field"] for c in brand_payload["columns"] if c["editable"]]
    assert editable_fields == ["BRAND"]

    r = client.get(f"/api/runs/{run_id}/qc/sheets/{pack_key}")
    assert r.status_code == 200
    pack_payload = r.json()
    assert pack_payload["attribute"] == "PACK_SIZE"
    editable_fields = [c["field"] for c in pack_payload["columns"] if c["editable"]]
    assert editable_fields == ["PACK_SIZE"]

    # The dropdown for BRAND must contain real brand labels and none of
    # the pack-size labels — and vice versa.  This is the user-visible
    # invariant of the QC wizard ("distinct dropdown values per table").
    brand_opts = set(brand_payload["attribute_options"])
    pack_opts  = set(pack_payload["attribute_options"])

    brand_labels = {"ACME", "ZETA", "OMEGA"}
    pack_labels  = {"12 OZ", "16 OZ", "24 OZ", "8 OZ", "32 OZ"}

    assert brand_labels & brand_opts, (
        f"BRAND dropdown is missing every expected label: {sorted(brand_opts)}"
    )
    assert pack_labels & pack_opts, (
        f"PACK_SIZE dropdown is missing every expected label: {sorted(pack_opts)}"
    )
    assert not (brand_opts & pack_labels), (
        f"BRAND dropdown leaked PACK_SIZE labels: {brand_opts & pack_labels}"
    )
    assert not (pack_opts & brand_labels), (
        f"PACK_SIZE dropdown leaked BRAND labels: {pack_opts & brand_labels}"
    )

    # Each list must be deduped — `set` round-trip won't shrink it.
    assert len(brand_payload["attribute_options"]) == len(set(brand_payload["attribute_options"]))
    assert len(pack_payload["attribute_options"])  == len(set(pack_payload["attribute_options"]))

    # ── 5. Submit an edit on the first row ────────────────────────────
    first_row = brand_payload["rows"][0]
    edited_value = "ZETA" if first_row["BRAND"] != "ZETA" else "ACME"
    r = client.put(
        f"/api/runs/{run_id}/qc/sheets/{brand_key}",
        json={"edited_rows": [{"row_id": first_row["_row_id"], "attribute_value": edited_value}]},
    )
    assert r.status_code == 204

    # Edit count surfaces in the summary list.
    r = client.get(f"/api/runs/{run_id}/qc/sheets")
    assert r.status_code == 200
    summary = next(s for s in r.json()["sheets"] if s["key"] == brand_key)
    assert summary["edited_count"] == 1

    # ── 6. Finalize → download → confirm xlsx is valid ────────────────
    r = client.post(f"/api/runs/{run_id}/qc/finalize")
    assert r.status_code == 200, r.text
    download_url = r.json()["download_url"]

    final_status = client.get(f"/api/runs/{run_id}").json()
    assert final_status["state"] == "done"
    assert final_status["progress"] == 1.0

    r = client.get(download_url)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    # xlsxwriter output is a zip archive — fail loudly if it's malformed
    # rather than letting a downstream parser blow up.
    assert zipfile.is_zipfile(io.BytesIO(r.content)), "downloaded xlsx is not a valid zip archive"
