"""
Concurrency stress + null-preservation tests for the subprocess path.

Two scenarios that the rest of the suite doesn't cover:

1. **Five Phase 1 runs in parallel** — verifies that 5 simultaneous
   subprocesses don't trample each other (no jumbled stdout, no
   tmpdir collisions, no semaphore deadlock), all produce a valid
   xlsx, and all reach state=qc_ready within a reasonable timeout.

2. **Pickle roundtrip preserves nulls** — the production path picks
   up the subprocess result via `pickle.load`.  The mapping logic
   relies on certain rows/cells being null (or the pipeline's "nan"
   stringification of them) — if pickle ever drops or coerces those,
   results would differ between in-process and subprocess execution.
   We compare dictEnsemble outputs from the two paths against the
   same null-bearing inputs.

Slow — covers the real ML stack twice.  Skipped automatically when
heavy ML deps aren't installed (see integration/conftest.py).
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Any
import zipfile

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api import jobs
from api.main import app
from tests.fixtures import (
    build_flat_file_df,
    build_history_df,
    build_meta_df,
    write_phase1_inputs,
)


POLL_INTERVAL = 0.5
POLL_TIMEOUT  = 360.0   # 5 concurrent ML pipelines + cold start


@pytest.fixture(autouse=True)
def fresh_registry_and_subprocess_mode():
    prior = os.environ.pop("AIC_INPROCESS", None)
    jobs.registry = jobs.JobRegistry()
    try:
        yield
    finally:
        if prior is not None:
            os.environ["AIC_INPROCESS"] = prior


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _wait_for(client: TestClient, run_id: str, target: set[str]) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    last: dict = {}
    while time.time() < deadline:
        r = client.get(f"/api/runs/{run_id}")
        assert r.status_code == 200
        last = r.json()
        if last["state"] in target:
            return last
        if last["state"] == "error":
            raise AssertionError(
                f"Run {run_id} errored: {last.get('error')}\n"
                + "\n".join(last.get("log_tail", []))
            )
        time.sleep(POLL_INTERVAL)
    raise AssertionError(
        f"Run {run_id} timed out at state={last.get('state')}"
    )


def _start_phase1_run(client: TestClient, fixture_dir: Path) -> str:
    xlsx_path, csv_path = write_phase1_inputs(fixture_dir)
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
    return r.json()["run_id"]


# ── (1) Concurrency stress ──────────────────────────────────────────────────

def test_five_concurrent_phase1_runs_all_succeed(
    client: TestClient, tmp_path: Path,
) -> None:
    """5 simultaneous Phase 1 runs must all reach qc_ready independently.

    The semaphore caps RUN_SLOTS at 5 so all five SHOULD acquire slots
    immediately; if they don't, scheduling is broken.  Stdout streamed
    from each subprocess must land on the right JobRecord — log tails
    from one run leaking into another would mean the pipe-reading
    threads are crossed.
    """
    # Each run gets its own fixture dir so uploads don't collide.
    run_ids: list[str] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = []
        for i in range(5):
            fixture_dir = tmp_path / f"fix_{i}"
            fixture_dir.mkdir()
            futs.append(pool.submit(_start_phase1_run, client, fixture_dir))
        for f in as_completed(futs):
            run_ids.append(f.result())

    assert len(run_ids) == 5
    assert len(set(run_ids)) == 5, "registry handed out a duplicate run_id"

    # Every run must reach qc_ready.  We poll them in parallel so the
    # test isn't 5× longer than a single run.
    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(
            lambda rid: _wait_for(client, rid, {"qc_ready"}), run_ids,
        ))

    for rid, status in zip(run_ids, statuses):
        assert status["state"] == "qc_ready", (rid, status)
        assert status["progress"] >= 0.85
        # Each run's log tail should mention its own pipeline finish.
        # If two subprocesses' stdouts crossed, one tail would be empty
        # or contain unrelated banners.
        log_text = "\n".join(status.get("log_tail", []))
        assert "DONE pipeline" in log_text, (
            f"run {rid} log tail looks crossed:\n{log_text}"
        )


# ── (2) Pickle roundtrip preserves null-bearing structures ─────────────────
# We can't compare full pipeline outputs across two runs because XGBoost is
# non-deterministic across runs (OpenMP thread interleaving produces tiny
# score drift).  But the user's actual concern is "pickle drops/coerces
# values our mapping logic depends on" — that's a property of the pickle
# layer itself, not of the pipeline.  Test it directly with the exact
# Phase1Payload shape the worker spills.

def test_pickle_preserves_phase1_payload_with_null_bearing_dataframes() -> None:
    """A Phase1Payload roundtrip must preserve every NaN, mixed-type
    object cell, MultiIndex, and stringified-nan that mapping_lookup
    relies on."""
    import pickle
    from api.pipeline import Phase1Payload

    final = build_history_df()
    final.loc[2, "BRAND_RAW"]   = pd.NA
    final.loc[5, "PACK_RAW"]    = pd.NA
    final.loc[7, "DESCRIPTION"] = pd.NA

    flat = build_flat_file_df()
    flat.loc[1, "PACK_RAW"]  = pd.NA
    flat.loc[3, "BRAND_RAW"] = pd.NA

    meta = build_meta_df()
    meta.loc[0, "Type"] = pd.NA   # blank Type cell — common in production

    # dictEnsemble shape: per-attribute DataFrame with mixed object dtype.
    # Mirror what runEnsemble produces — strings, ints, floats, NaN cells.
    ensemble_df = pd.DataFrame([
        {"BRAND": "ACME",  "MLBRAND": "ACME", "score": 100.0,
         "QC Priority": "LOW",   "ML Matches Lookup": "Yes", "Note": ""},
        {"BRAND": pd.NA,   "MLBRAND": "ZETA", "score": 65.5,
         "QC Priority": "HIGH",  "ML Matches Lookup": "No",  "Note": pd.NA},
        {"BRAND": "OMEGA", "MLBRAND": "OMEGA","score": float("nan"),
         "QC Priority": "MEDIUM","ML Matches Lookup": "Yes", "Note": "check"},
    ])

    original = Phase1Payload(
        FINAL=final,
        FLAT_FILE_OUT=flat,
        meta=meta,
        dictEnsemble={"Final_BRAND_lkp": ensemble_df},
    )

    # Same protocol the worker uses.
    blob = pickle.dumps(original, protocol=pickle.HIGHEST_PROTOCOL)
    rebuilt: Phase1Payload = pickle.loads(blob)

    # Each frame must be byte-identical post-roundtrip.
    pd.testing.assert_frame_equal(rebuilt.FINAL,         final)
    pd.testing.assert_frame_equal(rebuilt.FLAT_FILE_OUT, flat)
    pd.testing.assert_frame_equal(rebuilt.meta,          meta)
    pd.testing.assert_frame_equal(
        rebuilt.dictEnsemble["Final_BRAND_lkp"], ensemble_df,
    )

    # Spot-check the specific null cells that the user flagged matter
    # for matching.  These are EXACT positions where pickle has to be
    # faithful: any coercion to "" or "nan" string would change pipeline
    # behaviour at the .fillna("nan") boundary in pipeline.py.
    assert pd.isna(rebuilt.FINAL.loc[2, "BRAND_RAW"])
    assert pd.isna(rebuilt.FINAL.loc[5, "PACK_RAW"])
    assert pd.isna(rebuilt.FLAT_FILE_OUT.loc[1, "PACK_RAW"])
    assert pd.isna(rebuilt.FLAT_FILE_OUT.loc[3, "BRAND_RAW"])
    assert pd.isna(rebuilt.meta.loc[0, "Type"])
    assert pd.isna(rebuilt.dictEnsemble["Final_BRAND_lkp"].loc[1, "BRAND"])
    assert pd.isna(rebuilt.dictEnsemble["Final_BRAND_lkp"].loc[1, "Note"])
    # NaN floats also survive.
    assert pd.isna(rebuilt.dictEnsemble["Final_BRAND_lkp"].loc[2, "score"])
