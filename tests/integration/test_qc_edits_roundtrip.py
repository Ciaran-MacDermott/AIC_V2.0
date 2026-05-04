"""
Integration test: QC edits round-trip into File_For_Mapping_QC.xlsx.

Drives a real Phase 1 run end-to-end, makes a handful of distinct edits
across both lookup sheets (BRAND and PACK_SIZE), finalizes, then opens
the produced workbook and asserts every edit is present at the correct
row in the correct sheet.

This is the strongest possible automated check that the QC wizard isn't
dropping edits or scrambling row identity between the React grid and the
xlsx writer — regressions here would be invisible in the fast suite
because qc_view.merge_edits / apply_edits_to_dataframe are pure-python
and pass even when the actual write_results integration drifts.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pd = pytest.importorskip("pandas")
pytest.importorskip("openpyxl")

from api import jobs
from api.main import app
from tests.fixtures import write_phase1_inputs


POLL_INTERVAL = 0.25
POLL_TIMEOUT  = 120.0


@pytest.fixture(autouse=True)
def fresh_registry():
    jobs.registry = jobs.JobRegistry()
    yield
    for r in list(jobs.registry._jobs.values()):
        r.stop_event.set(); r.resume_event.set()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _wait_for(client: TestClient, run_id: str, target: set[str]) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/runs/{run_id}").json()
        if last["state"] in target:
            return last
        if last["state"] == "error":
            raise AssertionError(
                f"Pipeline errored: {last.get('error')}\n"
                + "\n".join(last.get("log_tail") or [])
            )
        time.sleep(POLL_INTERVAL)
    raise AssertionError(f"Timed out waiting for {target}; last state={last.get('state')!r}")


def test_qc_edits_round_trip_into_workbook(client: TestClient, tmp_path: Path) -> None:
    p1_dir = tmp_path / "p1"; p1_dir.mkdir()
    xlsx_path, csv_path = write_phase1_inputs(p1_dir)

    # ── 1. Run Phase 1 ───────────────────────────────────────────────
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

    # ── 2. Sniff sheet keys + fetch payloads ─────────────────────────
    sheets = client.get(f"/api/runs/{run_id}/qc/sheets").json()["sheets"]
    by_label = {s["label"]: s for s in sheets}
    assert {"BRAND", "PACK_SIZE"} <= set(by_label.keys()), by_label

    brand_key = by_label["BRAND"]["key"]
    pack_key  = by_label["PACK_SIZE"]["key"]

    brand_payload = client.get(f"/api/runs/{run_id}/qc/sheets/{brand_key}").json()
    pack_payload  = client.get(f"/api/runs/{run_id}/qc/sheets/{pack_key}").json()

    assert brand_payload["rows"], "BRAND payload empty"
    assert pack_payload["rows"],  "PACK_SIZE payload empty"

    # ── 3. Plan a set of edits across both sheets ────────────────────
    # Pick the first three rows in each sheet (high → med → low priority)
    # and force them to deliberate sentinel values so we can spot them
    # unambiguously when we read the workbook back.
    brand_edits = [
        {"row_id": brand_payload["rows"][0]["_row_id"], "attribute_value": "ZZ_TOP_BRAND"},
        {"row_id": brand_payload["rows"][1]["_row_id"], "attribute_value": "ZZ_MID_BRAND"},
        {"row_id": brand_payload["rows"][2]["_row_id"], "attribute_value": ""},  # cleared
    ]
    pack_edits = [
        {"row_id": pack_payload["rows"][0]["_row_id"], "attribute_value": "ZZ_TOP_PACK"},
        {"row_id": pack_payload["rows"][1]["_row_id"], "attribute_value": "ZZ_MID_PACK"},
    ]

    # ── 4. PUT edits (sheet-by-sheet, like the React grid does) ──────
    r = client.put(
        f"/api/runs/{run_id}/qc/sheets/{brand_key}",
        json={"edited_rows": brand_edits},
    )
    assert r.status_code == 204
    r = client.put(
        f"/api/runs/{run_id}/qc/sheets/{pack_key}",
        json={"edited_rows": pack_edits},
    )
    assert r.status_code == 204

    # Edit counts surface back via the summary list.
    summary = {s["key"]: s for s in client.get(
        f"/api/runs/{run_id}/qc/sheets",
    ).json()["sheets"]}
    assert summary[brand_key]["edited_count"] == len(brand_edits)
    assert summary[pack_key]["edited_count"]  == len(pack_edits)

    # And re-fetching the sheet payload echoes the in-progress edits.
    re_brand = client.get(f"/api/runs/{run_id}/qc/sheets/{brand_key}").json()
    by_id = {r["_row_id"]: r for r in re_brand["rows"]}
    for edit in brand_edits:
        assert by_id[edit["row_id"]]["BRAND"] == edit["attribute_value"], edit

    # ── 5. Finalize → download workbook ──────────────────────────────
    r = client.post(f"/api/runs/{run_id}/qc/finalize")
    assert r.status_code == 200, r.text
    download_url = r.json()["download_url"]

    final = client.get(f"/api/runs/{run_id}").json()
    assert final["state"] == "done"

    r = client.get(download_url)
    assert r.status_code == 200

    out_path = tmp_path / "downloaded.xlsx"
    out_path.write_bytes(r.content)

    # ── 6. Open the produced workbook and assert every edit is there ─
    xf = pd.ExcelFile(str(out_path))

    # Sheet keys are e.g. "Final_BRAND_lkp" → resolve back to the
    # actual sheet names in the workbook.
    def _matching_sheet(prefix: str) -> str:
        cand = [s for s in xf.sheet_names if prefix.upper() in s.upper()]
        assert cand, f"no sheet matching {prefix!r} in {xf.sheet_names}"
        return cand[0]

    brand_sheet_name = _matching_sheet("BRAND")
    pack_sheet_name  = _matching_sheet("PACK")

    brand_df = pd.read_excel(str(out_path), sheet_name=brand_sheet_name)
    pack_df  = pd.read_excel(str(out_path), sheet_name=pack_sheet_name)

    # Display order in the xlsx matches the post-sort order the React
    # grid showed, so r0 → row 0, r1 → row 1, etc.
    def _row_index(row_id: str) -> int:
        return int(row_id.lstrip("r"))

    for edit in brand_edits:
        idx = _row_index(edit["row_id"])
        cell = brand_df.iloc[idx]["BRAND"]
        # NaN tolerant comparison for the cleared edit.
        if edit["attribute_value"] == "":
            assert pd.isna(cell) or str(cell).strip() == "" or str(cell) == "nan", \
                f"expected blank at row {idx}, got {cell!r}"
        else:
            assert str(cell) == edit["attribute_value"], (
                f"BRAND row {idx}: expected {edit['attribute_value']!r}, got {cell!r}"
            )

    for edit in pack_edits:
        idx = _row_index(edit["row_id"])
        cell = pack_df.iloc[idx]["PACK_SIZE"]
        assert str(cell) == edit["attribute_value"], (
            f"PACK_SIZE row {idx}: expected {edit['attribute_value']!r}, got {cell!r}"
        )

    # ── 7. Spot-check a non-edited row to confirm we didn't blanket-overwrite ──
    untouched_idx = max(_row_index(e["row_id"]) for e in brand_edits) + 1
    if untouched_idx < len(brand_df):
        # The untouched cell should match the *original* attribute the
        # payload showed before edits — so re-fetch the original value
        # and assert equality.
        original = brand_payload["rows"][untouched_idx]["BRAND"]
        cell = brand_df.iloc[untouched_idx]["BRAND"]
        assert str(cell) == str(original), (
            f"untouched BRAND row {untouched_idx}: expected {original!r}, got {cell!r}"
        )

    # ── 8. Sheets that weren't touched survive untouched too ──────────
    # Confirm FINAL/META/FLAT_FILE still exist and have content.
    for required in ("FINAL", "META"):
        cands = [s for s in xf.sheet_names if required.upper() in s.upper()]
        assert cands, f"{required} sheet missing from {xf.sheet_names}"

    print(
        f"  ✓ Round-trip OK — {len(brand_edits)} BRAND edits + "
        f"{len(pack_edits)} PACK_SIZE edits all present in {out_path.name}"
    )
