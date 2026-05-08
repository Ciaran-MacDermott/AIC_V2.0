"""
Reproduce the TOOL_BRAND dropdown bug observed during user testing.

Setup (real zip the user uploaded to the running UI):
  C:\\Users\\Ciaran MacDermott\\Downloads\\AICP3_MultiCatTesting (2).zip

The zip is a multi-cat run with four sub-models (CLM_FOOD, CLM_MULO,
CLM_TGT, CLM_WALM).  In the UI, the analyst lands on the brand-mismatch
wizard at "TGT (3 of 4)" and sees a row with:

    BRAND_TGT = "AO BRANDS"
    TOOL_BRAND_TGT = "AO CLOROX RESTRICTED"
    RMRR = "RES"
    BRAND dropdown selected = "AO BRANDS"
    TOOL_BRAND dropdown selected = "—"   (empty option, blank fallback)

The test splits the diagnosis into two assertions so we know which side
of the system is at fault:

  (A) PIPELINE side: did step 12 (raw_multi_restricted_overrides)
      actually canonicalise TOOL_BRAND_TGT to include "AO CLOROX
      RESTRICTED" for at least one row?  Asserted by checking the TGT
      group's rows for that value.

  (B) UI/PAYLOAD side: does the wire payload's tool_brand_values list
      include "AO CLOROX RESTRICTED" so that the React <select> can
      actually display it as a selected option?

If (A) passes but (B) fails -> the dropdown options are sourced from the
wrong column (suspected: collect_dropdown_values reads only groups[0]
so suffixed groups never see their canonical values).
If (A) fails -> the bug is upstream of the dialog (pipeline didn't
apply the suffix).

Skipped automatically if heavy phase3 deps aren't installed or the zip
isn't present at the expected path.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import jobs
from api.main import app


ZIP_PATH = Path(
    r"C:\Users\Ciaran MacDermott\Downloads\AICP3_MultiCatTesting (2).zip"
)
POLL_TIMEOUT = 600.0
POLL_INTERVAL = 0.5


pytestmark = [
    pytest.mark.skipif(
        not ZIP_PATH.exists(),
        reason=f"Multi-cat fixture zip not present at {ZIP_PATH}",
    ),
    pytest.mark.skipif(
        pytest.importorskip("openpyxl", reason="openpyxl required") is None,
        reason="openpyxl required",
    ),
]


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


def _config_form() -> dict[str, str]:
    return {
        "config": (
            '{"raw_upc_pl_brand_col":"RAW_BRAND",'
            '"is_custom_collapse":false,'
            '"skip_rmrr":false}'
        ),
    }


def test_tgt_group_dropdown_contains_restricted_canonical(
    client: TestClient,
    tmp_path: Path,
) -> None:
    # Copy zip into tmp so we don't risk touching the user's original.
    local_zip = tmp_path / "phase2.zip"
    shutil.copyfile(ZIP_PATH, local_zip)

    with local_zip.open("rb") as fh:
        r = client.post(
            "/api/phase2/runs",
            files={"zip": ("phase2.zip", fh, "application/zip")},
            data=_config_form(),
        )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    status = _wait_for(client, run_id, {"mismatch_pending", "done"})
    assert status["state"] == "mismatch_pending", (
        f"Expected the multi-cat run to surface a mismatch dialog "
        f"(it does in the UI), but ended up at state={status['state']}"
    )

    payload = client.get(f"/api/runs/{run_id}/mismatch").json()
    groups = payload["groups"]
    tool_brand_values: list[str] = payload.get("tool_brand_values", [])
    brand_values: list[str] = payload.get("brand_values", [])

    # Diagnostic dump — printed only on failure / -s mode.
    print("\n=== MISMATCH PAYLOAD SUMMARY ===")
    print(f"groups: {len(groups)}")
    for i, g in enumerate(groups):
        print(
            f"  [{i}] suffix={g['model_suffix']!r:>10}  "
            f"brand_col={g['brand_col']:<20}  tool_brand_col={g['tool_brand_col']}"
        )
    print(f"brand_values     ({len(brand_values)} total): "
          f"{brand_values[:8]}{'...' if len(brand_values) > 8 else ''}")
    print(f"tool_brand_values({len(tool_brand_values)} total): "
          f"{tool_brand_values[:8]}{'...' if len(tool_brand_values) > 8 else ''}")

    # ------------------------------------------------------------------
    # (A) Pipeline-side assertion: step 12 should have canonicalised at
    # least one TOOL_BRAND_* to "<base> RESTRICTED" before the mismatch
    # check ran.  Loop every group so the test doesn't depend on which
    # sub-model produced the suffixed row.
    # ------------------------------------------------------------------
    pipeline_canonicalised_some = any(
        (row.get("TOOL_BRAND") or "").upper().endswith(" RESTRICTED")
        for g in groups for row in g["rows"]
    )
    assert pipeline_canonicalised_some, (
        "PIPELINE BUG: step 12 (raw_multi_restricted_overrides) did NOT "
        "produce any 'X RESTRICTED' values in any mismatch group's "
        "TOOL_BRAND."
    )

    # ------------------------------------------------------------------
    # (B) UI-side assertion: the dropdown options must include EVERY
    # BRAND / TOOL_BRAND value across EVERY group.  The current bug is
    # that collect_dropdown_values only reads groups[0]'s columns, so
    # values that exist only in suffixed groups (e.g. TGT) are missing
    # and the React <select> falls back to the empty option.
    # ------------------------------------------------------------------
    all_tool_brands: set[str] = set()
    all_brands: set[str] = set()
    for g in groups:
        for row in g["rows"]:
            tb = row.get("TOOL_BRAND") or ""
            b = row.get("BRAND") or ""
            if tb:
                all_tool_brands.add(tb)
            if b:
                all_brands.add(b)

    missing_tb = sorted(v for v in all_tool_brands if v not in tool_brand_values)
    missing_b  = sorted(v for v in all_brands      if v not in brand_values)

    print(f"\nAll groups: {len(all_brands)} distinct BRAND, "
          f"{len(all_tool_brands)} distinct TOOL_BRAND")
    if missing_tb:
        print(f"TOOL_BRAND values missing from dropdown: {missing_tb}")
    if missing_b:
        print(f"BRAND values missing from dropdown: {missing_b}")

    assert not missing_tb and not missing_b, (
        "UI BUG: mismatch rows reference values that are NOT in the "
        "dropdown lists — the React <select> renders blank (em-dash) "
        "for those rows.\n"
        f"  TOOL_BRAND missing: {missing_tb}\n"
        f"  BRAND missing:      {missing_b}\n"
        f"  groups[0] is suffix={groups[0]['model_suffix']!r} "
        f"(b_col={groups[0]['brand_col']}, "
        f"tb_col={groups[0]['tool_brand_col']}) — "
        "collect_dropdown_values reads only groups[0]'s columns."
    )
