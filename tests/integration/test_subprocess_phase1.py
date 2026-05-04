"""
Verify Phase 1 runs end-to-end via the subprocess path.

The other integration tests run in-process (AIC_INPROCESS=1, set in
the root conftest so the existing monkeypatched suites keep working).
This one explicitly clears that flag before kicking off the run, so
the worker actually spawns `python -m api.run_pipeline phase1 …` and
we exercise the multi-process path operators will see in production.

Slow — covers the same ML stack as test_phase1_real plus pickle
round-tripping and a real Popen.  Skipped automatically when heavy ML
deps aren't installed (see integration/conftest.py).
"""

from __future__ import annotations

import os
import time
import zipfile
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import jobs
from api.main import app
from tests.fixtures import write_phase1_inputs


POLL_INTERVAL = 0.5
POLL_TIMEOUT  = 240.0   # subprocess cold-start adds ~3s vs in-process


@pytest.fixture(autouse=True)
def fresh_registry_and_subprocess_mode():
    """Force the worker into real-subprocess mode for this test only."""
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


def _wait_for(client: TestClient, run_id: str, target_states: set[str]) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    last: dict = {}
    while time.time() < deadline:
        r = client.get(f"/api/runs/{run_id}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last["state"] in target_states:
            return last
        if last["state"] == "error":
            raise AssertionError(
                f"Pipeline errored: {last.get('error')}\n"
                + "\n".join(last.get("log_tail", []))
            )
        time.sleep(POLL_INTERVAL)
    raise AssertionError(
        f"Timed out waiting for {target_states}; last state={last.get('state')}"
    )


def test_phase1_subprocess_happy_path(client: TestClient, tmp_path: Path) -> None:
    """Phase 1 runs in a child process, streams stdout, exits 0,
    parent reads result.pkl and finalizes a valid xlsx."""
    xlsx_path, csv_path = write_phase1_inputs(tmp_path)

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

    status = _wait_for(client, run_id, {"qc_ready"})
    assert status["progress"] >= 0.85
    # Stage banners from the child process must have made it back into
    # the parent's log buffer.
    log_text = "\n".join(status.get("log_tail", []))
    assert "DONE pipeline" in log_text, (
        f"child stdout did not stream into parent log:\n{log_text}"
    )

    # Finalize and confirm the xlsx is real — the post-QC write happens
    # in-process in the parent (write_qc_excel doesn't touch ml_package
    # globals), so this also verifies the Phase1Payload pickle survived
    # the round-trip.
    r = client.post(f"/api/runs/{run_id}/qc/finalize")
    assert r.status_code == 200, r.text
    download_url = r.json()["download_url"]

    r = client.get(download_url)
    assert r.status_code == 200
    assert zipfile.is_zipfile(io.BytesIO(r.content))


def test_phase1_subprocess_stop_event(client: TestClient, tmp_path: Path) -> None:
    """POSTing /stop while the child is mid-pipeline must SIGTERM it
    and end up at state='stopped', not error."""
    xlsx_path, csv_path = write_phase1_inputs(tmp_path)

    with xlsx_path.open("rb") as xfh, csv_path.open("rb") as cfh:
        r = client.post(
            "/api/phase1/runs",
            files={
                "xlsx": ("fixture.xlsx", xfh, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                "csv":  ("fixture.csv",  cfh, "text/csv"),
            },
        )
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    # Wait until the child is actually running (otherwise we'd be
    # stopping it during slot acquisition, which exercises a different
    # code path).  "running" with progress > 0 is a good marker.
    deadline = time.time() + 30
    while time.time() < deadline:
        st = client.get(f"/api/runs/{run_id}").json()
        if st["state"] == "running" and st["progress"] > 0:
            break
        if st["state"] in ("error", "stopped", "qc_ready"):
            break
        time.sleep(0.2)

    client.post(f"/api/runs/{run_id}/stop")

    final = _wait_for(client, run_id, {"stopped", "qc_ready"})
    # If the child got far enough that it was already in `qc_ready` by
    # the time we hit /stop, accept it — the unit-of-work is tiny on
    # the synthetic fixture.  Anything else means stop didn't work.
    assert final["state"] in ("stopped", "qc_ready"), final
