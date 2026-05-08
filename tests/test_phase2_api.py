"""
Fast Phase 2 route tests.

The adapter functions (run_phase_a / run_phase_b) are monkeypatched so
the route + state-machine logic can be exercised without spinning up a
real Phase 3 pipeline.  The integration suite covers the real path end
to end.
"""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

from fastapi.testclient import TestClient

from api import jobs
from api.main import app
from api.pipeline_phase2 import (
    MismatchReviewNeeded,
    Phase2InterimState,
    Phase2Result,
)


WAIT_TIMEOUT = 5.0
WAIT_SLEEP   = 0.02


@pytest.fixture(autouse=True)
def fresh_registry():
    jobs.registry = jobs.JobRegistry()
    yield
    # Teardown: any workers still parked on resume_event must be released
    # so they don't hold PIPELINE_LOCK across tests.
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
        time.sleep(WAIT_SLEEP)
    raise AssertionError(f"timed out waiting: {msg}")


def _make_zip_bytes() -> bytes:
    """Tiny zip — content irrelevant because we stub the pipeline."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ModelInfo.txt", "model")
        zf.writestr("Attributes.txt", "attrs")
    return buf.getvalue()


def _default_config() -> dict:
    return {
        "raw_upc_pl_brand_col": "RAW_BRAND",
        "private_label_config": {
            "walmart": {"enabled": True,  "label": "PRIVATE LABEL RESTRICTED"},
        },
        "brand_override_config": {
            "enable": False,
            "raw_manufacturer_col": "RAW_MANUFACTURER",
            "brand_col": "BRAND",
            "tool_brand_col": "TOOL_BRAND",
            "rules": [],
        },
        "is_custom_collapse": False,
        "skip_rmrr": False,
        "pl_base_name": "",
    }


# ── Happy path: no mismatches, run completes straight through ───────────────

def test_phase2_happy_path(client: TestClient, monkeypatch, tmp_path: Path) -> None:
    interim = Phase2InterimState(
        df=pd.DataFrame({"x": [1]}),
        duplicate_dimkeys=pd.DataFrame(),
        pipeline_context={},
    )

    def fake_phase_a(directory_path, inputs, stop_event=None):
        return interim

    def fake_phase_b(state, corrections, output_dir, stop_event=None):
        out_path = output_dir / "output.xlsx"
        out_path.write_bytes(b"FAKEXLSX")
        return Phase2Result(
            collapsed_df=state.df,
            duplicate_dimkeys=state.duplicate_dimkeys,
            output_xlsx_path=out_path,
        )

    from api import worker as worker_mod
    monkeypatch.setattr(worker_mod, "run_phase_a", fake_phase_a)
    monkeypatch.setattr(worker_mod, "run_phase_b", fake_phase_b)

    r = client.post(
        "/api/phase2/runs",
        files={"zip": ("input.zip", _make_zip_bytes(), "application/zip")},
        data={"config": json.dumps(_default_config())},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    _wait_until(
        lambda: jobs.registry.get(run_id).state == "done",
        msg="phase 2 worker did not reach state=done",
    )

    status = client.get(f"/api/runs/{run_id}").json()
    assert status["state"] == "done"
    assert status["progress"] == 1.0
    assert status["mismatch_count"] is None

    r = client.get(f"/api/runs/{run_id}/artifacts/output.xlsx")
    assert r.status_code == 200
    assert r.content == b"FAKEXLSX"


# ── Mismatch path: pause, get groups, resolve, finish ──────────────────────

def test_phase2_mismatch_pause_and_resume(
    client: TestClient, monkeypatch, tmp_path: Path,
) -> None:
    interim = Phase2InterimState(
        df=pd.DataFrame({"x": [1]}),
        duplicate_dimkeys=pd.DataFrame(),
        pipeline_context={},
    )
    mismatch_groups = [{
        "model_suffix":   "",
        "brand_col":      "BRAND",
        "tool_brand_col": "TOOL_BRAND",
        "mismatch_df":    pd.DataFrame([
            {"BRAND": "ACME", "TOOL_BRAND": "ACMI"},
        ]),
        "parent_col":     None,
    }]

    received_corrections: list = []

    def fake_phase_a(directory_path, inputs, stop_event=None):
        raise MismatchReviewNeeded(groups=mismatch_groups, phase_a_state=interim)

    def fake_phase_b(state, corrections, output_dir, stop_event=None):
        received_corrections.extend(corrections)
        out_path = output_dir / "output.xlsx"
        out_path.write_bytes(b"AFTER_REVIEW")
        return Phase2Result(
            collapsed_df=state.df,
            duplicate_dimkeys=state.duplicate_dimkeys,
            output_xlsx_path=out_path,
        )

    from api import worker as worker_mod
    monkeypatch.setattr(worker_mod, "run_phase_a", fake_phase_a)
    monkeypatch.setattr(worker_mod, "run_phase_b", fake_phase_b)

    r = client.post(
        "/api/phase2/runs",
        files={"zip": ("input.zip", _make_zip_bytes(), "application/zip")},
        data={"config": json.dumps(_default_config())},
    )
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    _wait_until(
        lambda: jobs.registry.get(run_id).state == "mismatch_pending",
        msg="worker did not park on mismatch_pending",
    )

    # Status surfaces the count.
    status = client.get(f"/api/runs/{run_id}").json()
    assert status["state"] == "mismatch_pending"
    assert status["mismatch_count"] == 1

    # Mismatch endpoint returns the JSON-safe shape.
    r = client.get(f"/api/runs/{run_id}/mismatch")
    assert r.status_code == 200
    body = r.json()
    assert len(body["groups"]) == 1
    assert body["groups"][0]["brand_col"] == "BRAND"
    row = body["groups"][0]["rows"][0]
    assert row["BRAND"] == "ACME" and row["TOOL_BRAND"] == "ACMI"
    # Enrichment columns added by serialise_mismatch_groups.
    assert row["BRAND_NEW"] == "ACME"
    assert row["TOOL_BRAND_NEW"] == "ACMI"

    # Resolve with a single correction and confirm Phase B runs with it.
    r = client.post(
        f"/api/runs/{run_id}/mismatch/resolve",
        json={"corrections": [{
            "type":           "tool_brand",
            "brand":          "ACME",
            "tool_brand_old": "ACMI",
            "tool_brand_new": "ACME",
            "brand_col":      "BRAND",
            "tool_brand_col": "TOOL_BRAND",
        }]},
    )
    assert r.status_code == 200, r.text

    _wait_until(
        lambda: jobs.registry.get(run_id).state == "done",
        msg="worker did not reach state=done after resume",
    )

    assert len(received_corrections) == 1
    assert received_corrections[0]["tool_brand_new"] == "ACME"

    r = client.get(f"/api/runs/{run_id}/artifacts/output.xlsx")
    assert r.status_code == 200
    assert r.content == b"AFTER_REVIEW"


# ── Stop-while-paused: resolve endpoint won't accept, run goes to stopped ──

def test_phase2_stop_while_paused(client: TestClient, monkeypatch, tmp_path: Path) -> None:
    interim = Phase2InterimState(
        df=pd.DataFrame({"x": [1]}),
        duplicate_dimkeys=pd.DataFrame(),
        pipeline_context={},
    )
    groups = [{
        "model_suffix":   "",
        "brand_col":      "BRAND",
        "tool_brand_col": "TOOL_BRAND",
        "mismatch_df":    pd.DataFrame([{"BRAND": "ACME", "TOOL_BRAND": "ACMI"}]),
        "parent_col":     None,
    }]

    def fake_phase_a(directory_path, inputs, stop_event=None):
        raise MismatchReviewNeeded(groups=groups, phase_a_state=interim)

    def never_called_phase_b(*a, **kw):
        raise AssertionError("phase B must not run when stopped during pause")

    from api import worker as worker_mod
    monkeypatch.setattr(worker_mod, "run_phase_a", fake_phase_a)
    monkeypatch.setattr(worker_mod, "run_phase_b", never_called_phase_b)

    r = client.post(
        "/api/phase2/runs",
        files={"zip": ("input.zip", _make_zip_bytes(), "application/zip")},
        data={"config": json.dumps(_default_config())},
    )
    run_id = r.json()["run_id"]

    _wait_until(
        lambda: jobs.registry.get(run_id).state == "mismatch_pending",
        msg="worker did not park on mismatch_pending",
    )

    r = client.post(f"/api/runs/{run_id}/stop")
    assert r.status_code == 204

    _wait_until(
        lambda: jobs.registry.get(run_id).state == "stopped",
        msg="worker did not reach state=stopped",
    )


def test_phase2_review_timeout_marks_run_stopped(
    client: TestClient, monkeypatch, tmp_path: Path
) -> None:
    """When the analyst never resolves a mismatch_pending review, the
    parked worker must time out, set state=stopped, and exit so the
    record becomes evictable by idle-TTL."""
    interim = Phase2InterimState(
        df=pd.DataFrame({"x": [1]}),
        duplicate_dimkeys=pd.DataFrame(),
        pipeline_context={},
    )
    groups = [{
        "model_suffix":   "",
        "brand_col":      "BRAND",
        "tool_brand_col": "TOOL_BRAND",
        "mismatch_df":    pd.DataFrame([{"BRAND": "ACME", "TOOL_BRAND": "ACMI"}]),
        "parent_col":     None,
    }]

    def fake_phase_a(directory_path, inputs, stop_event=None):
        raise MismatchReviewNeeded(groups=groups, phase_a_state=interim)

    def never_called_phase_b(*a, **kw):
        raise AssertionError("phase B must not run when review times out")

    from api import worker as worker_mod
    monkeypatch.setattr(worker_mod, "run_phase_a", fake_phase_a)
    monkeypatch.setattr(worker_mod, "run_phase_b", never_called_phase_b)
    # Tiny timeout so the test runs in milliseconds, not 2 hours.
    monkeypatch.setattr(worker_mod, "MISMATCH_REVIEW_TIMEOUT_S", 0.1)

    r = client.post(
        "/api/phase2/runs",
        files={"zip": ("input.zip", _make_zip_bytes(), "application/zip")},
        data={"config": json.dumps(_default_config())},
    )
    run_id = r.json()["run_id"]

    _wait_until(
        lambda: jobs.registry.get(run_id).state == "stopped",
        msg="worker did not reach state=stopped after review timeout",
    )

    record = jobs.registry.get(run_id)
    log_text = "\n".join(record.log_lines)
    assert "abandoned" in log_text.lower(), log_text


def test_phase2_invalid_zip_extension(client: TestClient) -> None:
    r = client.post(
        "/api/phase2/runs",
        files={"zip": ("notes.txt", b"x", "text/plain")},
        data={"config": json.dumps(_default_config())},
    )
    assert r.status_code == 400


def test_phase2_invalid_config_json(client: TestClient) -> None:
    r = client.post(
        "/api/phase2/runs",
        files={"zip": ("input.zip", _make_zip_bytes(), "application/zip")},
        data={"config": "{not json"},
    )
    assert r.status_code == 400


def test_get_mismatch_409_when_not_paused(client: TestClient, tmp_path: Path) -> None:
    d = tmp_path / "p2"
    d.mkdir()
    record = jobs.registry.create(phase="phase2", tmpdir=d)
    jobs.set_state(record, state="running")

    r = client.get(f"/api/runs/{record.run_id}/mismatch")
    assert r.status_code == 409


def test_resolve_409_when_not_paused(client: TestClient, tmp_path: Path) -> None:
    d = tmp_path / "p2"
    d.mkdir()
    record = jobs.registry.create(phase="phase2", tmpdir=d)
    jobs.set_state(record, state="done")

    r = client.post(
        f"/api/runs/{record.run_id}/mismatch/resolve",
        json={"corrections": []},
    )
    assert r.status_code == 409


def test_resolve_double_submit_second_request_409s(
    client: TestClient, tmp_path: Path,
) -> None:
    """
    Two near-simultaneous resolve POSTs (network retry, double-click,
    tab duplication): the first must apply its corrections, the second
    must 409 deterministically rather than silently overwriting them.

    Regression for the race where the state guard ran outside the lock
    so both requests passed through and the second clobbered the first.
    """
    d = tmp_path / "p2"
    d.mkdir()
    record = jobs.registry.create(phase="phase2", tmpdir=d)
    jobs.set_state(record, state="mismatch_pending")

    first = client.post(
        f"/api/runs/{record.run_id}/mismatch/resolve",
        json={"corrections": [
            {"type": "tool_brand", "brand": "AO BRANDS",
             "tool_brand_old": "AO CLOROX RESTRICTED",
             "tool_brand_new": "AO CLOROX",
             "brand_col": "BRAND_TGT", "tool_brand_col": "TOOL_BRAND_TGT"},
        ]},
    )
    assert first.status_code == 200

    # State must have flipped to "running" inside the locked block of the
    # first call — second request sees that and 409s.
    assert record.state == "running"

    second = client.post(
        f"/api/runs/{record.run_id}/mismatch/resolve",
        json={"corrections": [
            {"type": "tool_brand", "brand": "AO BRANDS",
             "tool_brand_old": "AO CLOROX RESTRICTED",
             "tool_brand_new": "DIFFERENT VALUE",
             "brand_col": "BRAND_TGT", "tool_brand_col": "TOOL_BRAND_TGT"},
        ]},
    )
    assert second.status_code == 409

    # First request's corrections survive intact; second's payload is
    # not on the record.
    saved = record.mismatch_corrections
    assert len(saved) == 1
    assert saved[0]["tool_brand_new"] == "AO CLOROX"


def test_phase2_zip_extraction_unwraps_single_top_folder(tmp_path: Path) -> None:
    """Direct unit test for extract_input_zip — covers the wrapper-folder case."""
    from api.pipeline_phase2 import extract_input_zip

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("project_root/ModelInfo.txt",     "data")
        zf.writestr("project_root/Attributes.txt",    "attrs")
        zf.writestr("project_root/sub/AttributeValues.txt", "v")

    dest = tmp_path / "extracted"
    effective = extract_input_zip(buf.getvalue(), dest)
    assert effective.name == "project_root"
    assert (effective / "ModelInfo.txt").exists()
    assert (effective / "sub" / "AttributeValues.txt").exists()
