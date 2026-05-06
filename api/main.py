"""
FastAPI BFF for the AIC Phase 1 refactor.

In dev: Next runs on :3000 and hits this on :8000 (CORS allows it).
In prod: `npm run build` produces web/out/, FastAPI serves it at /, and
the whole app runs on a single port — same shape as data_ingester.
"""

from __future__ import annotations

import io
import json
import os
import pickle
import shutil
import sys
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api import jobs, qc_view, worker
from api.inputs import (
    InputError,
    extract_zip_with_unwrap,
    find_phase1_inputs,
    scan_phase2_directory,
    scan_phase2_xlsx,
)
from api.pipeline import write_qc_excel
from api.pipeline_phase2 import Phase2Inputs, extract_input_zip
from api.schemas import (
    ActiveRuns,
    ActiveRunSummary,
    JobStatus,
    LogChunk,
    MismatchGroup,
    MismatchPayload,
    MismatchResolve,
    Phase2Config,
    Phase2Done,
    Phase2ScanResult,
    PostQcDone,
    QcEditPayload,
    QcFinalized,
    QcSheetList,
    QcSheetPayload,
    RunCreated,
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """
    Graceful-shutdown plumbing.

    On SIGTERM / SIGINT (uvicorn's default reload, container stop, ctrl-c)
    FastAPI runs the post-yield branch.  We:
      • signal every running JobRecord to stop and unpark any worker
        sleeping on resume_event (Phase 2 mismatch review)
      • give the worker threads a few seconds to observe the stop and
        let their subprocesses exit cleanly (the subprocess sees
        SIGTERM and converts it to ExitCode.STOPPED)

    We don't try to wait for every worker to fully drain — uvicorn caps
    its own grace window — but signalling them lets the most common
    case (idle slot, pipeline mid-stage) reach a terminal state instead
    of being torn out from under the analyst.
    """
    yield
    _signal_running_workers_stop()


def _signal_running_workers_stop() -> None:
    for record in jobs.registry.list_active():
        if record.state in {"queued", "running", "finalizing",
                            "post_qc_running", "mismatch_pending"}:
            record.stop_event.set()
            record.resume_event.set()


app = FastAPI(title="AIC API", lifespan=_lifespan)

# CORS:
#   * Dev: Next runs on :3000 and the BFF on :8000 — pre-flight needs to
#     pass for cross-origin fetch to work.
#   * Prod (single Docker container): the static export is served by
#     FastAPI itself, so requests are same-origin and CORS is moot.
#   * Other deployments (frontend behind a separate domain) can extend
#     the allow-list via AIC_CORS_ORIGINS=https://foo,https://bar.
_extra_origins = [
    o.strip() for o in os.environ.get("AIC_CORS_ORIGINS", "").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", *_extra_origins],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Upload size guard.  Without this a 5 GB upload reads straight into
# memory inside the route handler (`await xlsx.read()`) and OOMs the
# BFF.  200 MB is comfortably above the largest realistic Phase 1
# input we've seen and well under the per-process headroom.  Override
# via env var if a workflow ever needs more.
MAX_UPLOAD_BYTES = int(os.environ.get("AIC_MAX_UPLOAD_MB", "200")) * 1024 * 1024


@app.middleware("http")
async def _enforce_upload_limit(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            status_code=413,
            content={
                "error_title":    "File too large",
                "error_advice":   (
                    f"This upload is over the {MAX_UPLOAD_BYTES // (1024*1024)} MB "
                    "per-request limit.  If your input is genuinely this big, "
                    "ask an admin to raise AIC_MAX_UPLOAD_MB."
                ),
                "error_category": "input",
            },
        )
    return await call_next(request)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Tiny input-validation helpers ────────────────────────────────────────────
# Most Phase 1 / Phase 2 routes share the same shape: check a filename
# extension, parse a Phase2Config form field.  Hoisting both as named
# helpers keeps each route to its actual job (extract → start worker).

def _require_xlsx(upload: UploadFile, *, field: str = "xlsx") -> None:
    name = (upload.filename or "").lower()
    if not (name.endswith(".xlsx") or name.endswith(".xls")):
        raise HTTPException(400, f"{field} file must be .xlsx or .xls")


def _require_zip(upload: UploadFile, *, field: str = "zip") -> None:
    name = (upload.filename or "").lower()
    if not name.endswith(".zip"):
        raise HTTPException(400, f"{field} file must be .zip")


def _parse_phase2_config(config: str) -> Phase2Config:
    try:
        return Phase2Config.model_validate_json(config)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid config JSON: {exc}")


# ── Phase 1 lifecycle ────────────────────────────────────────────────────────

@app.post("/api/phase1/runs", response_model=RunCreated)
async def create_phase1_run(
    xlsx: UploadFile = File(...),
    csv:  UploadFile = File(...),
) -> RunCreated:
    _require_xlsx(xlsx)
    if not csv.filename or not csv.filename.endswith(".csv"):
        raise HTTPException(400, "csv file must be .csv")

    tmpdir = Path(tempfile.mkdtemp(prefix="aic_"))
    xlsx_path = tmpdir / xlsx.filename
    csv_path  = tmpdir / csv.filename
    xlsx_path.write_bytes(await xlsx.read())
    csv_path.write_bytes(await csv.read())

    record = jobs.registry.create(phase="phase1", tmpdir=tmpdir)
    worker.start_phase1(record, str(xlsx_path), str(csv_path))
    return RunCreated(run_id=record.run_id)


@app.post("/api/phase1/runs/zip", response_model=RunCreated)
async def create_phase1_run_from_zip(zip: UploadFile = File(...)) -> RunCreated:
    """
    Single-zip Phase 1 upload.  Mirrors the 'ZIP — full pipeline' radio
    option in 1_Phase_1_Attribute_Mapping.py: the archive must contain
    an Excel with META + FINAL sheets and a .csv flat file (anywhere in
    the tree, including a single top-level wrapper folder).

    Any txt files (ModelInfo / Attributes / AttributeValues) are kept
    on disk so a Phase 2 run started from this run's tmpdir can reuse
    them without re-uploading.
    """
    _require_zip(zip)

    tmpdir = Path(tempfile.mkdtemp(prefix="aic_p1_"))
    try:
        root = extract_zip_with_unwrap(await zip.read(), tmpdir / "extracted")
        xlsx_path, csv_path = find_phase1_inputs(root)
    except InputError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, str(exc))

    record = jobs.registry.create(phase="phase1", tmpdir=tmpdir)
    worker.start_phase1(record, str(xlsx_path), str(csv_path))
    return RunCreated(run_id=record.run_id)


# ── Run status / logs / control ──────────────────────────────────────────────

@app.get("/api/runs", response_model=ActiveRuns)
def list_runs() -> ActiveRuns:
    """
    Snapshot of all live runs, sorted oldest-first.

    Used by the dashboard widget so users dropping in mid-day can see
    what else is on the server before kicking off a new run — gives
    them a chance to wait for an in-flight phase2 instead of racing
    for the lock.
    """
    now = time.time()
    runs = sorted(jobs.registry.list_active(), key=lambda r: r.started_at)
    return ActiveRuns(runs=[
        ActiveRunSummary(
            run_id        = r.run_id,
            phase         = r.phase,
            state         = r.state,
            stage_label   = r.stage_label,
            progress      = r.progress,
            started_at    = r.started_at,
            elapsed_s     = (r.finished_at or now) - r.started_at,
            parent_run_id = r.parent_run_id,
        )
        for r in runs
    ])


@app.get("/api/runs/{run_id}", response_model=JobStatus)
def get_run(run_id: str) -> JobStatus:
    record = jobs.registry.get(run_id)
    if record is None:
        raise HTTPException(404, "Run not found")

    snap = jobs.snapshot(record)
    elapsed = (snap["finished_at"] or time.time()) - snap["started_at"]
    # snapshot() returns a dict with the same field names JobStatus expects;
    # the only field not on the snapshot is the derived elapsed_s.
    return JobStatus(elapsed_s=elapsed, **{
        k: v for k, v in snap.items() if k != "finished_at"
    })


@app.get("/api/runs/{run_id}/logs", response_model=LogChunk)
def get_logs(run_id: str, since: int = 0) -> LogChunk:
    record = jobs.registry.get(run_id)
    if record is None:
        raise HTTPException(404, "Run not found")
    cursor, lines = jobs.logs_since(record, since)
    return LogChunk(cursor=cursor, lines=lines)


@app.post("/api/runs/{run_id}/stop", status_code=204)
def stop_run(run_id: str) -> None:
    record = jobs.registry.get(run_id)
    if record is None:
        raise HTTPException(404, "Run not found")
    record.stop_event.set()
    # If the worker is parked on resume_event waiting for mismatch
    # corrections, wake it up so it can observe the stop and exit.
    record.resume_event.set()


@app.delete("/api/runs/{run_id}", status_code=204)
def delete_run(run_id: str) -> None:
    if not jobs.registry.delete(run_id):
        raise HTTPException(404, "Run not found")


# ── QC wizard ────────────────────────────────────────────────────────────────

def _require_qc_ready(run_id: str) -> jobs.JobRecord:
    record = jobs.registry.get(run_id)
    if record is None:
        raise HTTPException(404, "Run not found")
    if record.pipeline_payload is None:
        raise HTTPException(409, f"Run is in state '{record.state}', no QC payload yet")
    return record


@app.get("/api/runs/{run_id}/qc/sheets", response_model=QcSheetList)
def qc_sheets(run_id: str) -> QcSheetList:
    record = _require_qc_ready(run_id)
    summaries = qc_view.sheet_summaries(
        record.pipeline_payload["dictEnsemble"], record.qc_edits,
    )
    return QcSheetList(sheets=summaries)


@app.get("/api/runs/{run_id}/qc/sheets/{sheet_key}", response_model=QcSheetPayload)
def qc_sheet(run_id: str, sheet_key: str) -> QcSheetPayload:
    record = _require_qc_ready(run_id)
    dict_ensemble = record.pipeline_payload["dictEnsemble"]
    if sheet_key not in dict_ensemble:
        raise HTTPException(404, f"No QC sheet '{sheet_key}'")
    return qc_view.build_sheet_payload(
        sheet_key, dict_ensemble[sheet_key], record.qc_edits.get(sheet_key, {}),
    )


@app.put("/api/runs/{run_id}/qc/sheets/{sheet_key}", status_code=204)
def qc_save(run_id: str, sheet_key: str, payload: QcEditPayload) -> None:
    record = _require_qc_ready(run_id)
    if sheet_key not in record.pipeline_payload["dictEnsemble"]:
        raise HTTPException(404, f"No QC sheet '{sheet_key}'")
    qc_view.merge_edits(record.qc_edits, sheet_key, payload)


@app.post("/api/runs/{run_id}/qc/finalize", response_model=QcFinalized)
def qc_finalize(run_id: str) -> QcFinalized:
    record = _require_qc_ready(run_id)

    jobs.set_state(record, state="finalizing", stage_label="Writing QC workbook…")

    # Heavy frames live on disk between qc_ready and finalize so they
    # don't pin parent memory during the analyst's review.  Single
    # pickle.load here (~10–50 ms typical) and we have everything.
    from api.pipeline import Phase1Payload
    with open(record.pipeline_payload["_heavy_path"], "rb") as f:
        heavy = pickle.load(f)
    payload = Phase1Payload(
        FINAL=heavy["FINAL"],
        FLAT_FILE_OUT=heavy["FLAT_FILE_OUT"],
        meta=heavy["meta"],
        dictEnsemble=record.pipeline_payload["dictEnsemble"],
    )

    edited_dfs: dict = {}
    for sheet_key, edits in record.qc_edits.items():
        if not edits:
            continue
        edited_dfs[sheet_key] = qc_view.apply_edits_to_dataframe(
            sheet_key, payload.dictEnsemble[sheet_key], edits,
        )

    out_path = record.tmpdir / "File_For_Mapping_QC.xlsx"
    write_qc_excel(str(out_path), payload, edited_dfs)
    record.output_path = out_path

    jobs.set_state(
        record, state="done", progress=1.0, stage_label="✓ Complete",
    )
    return QcFinalized(download_url=f"/api/runs/{run_id}/artifacts/qc.xlsx")


# ── Artifact download ────────────────────────────────────────────────────────

@app.get("/api/runs/{run_id}/artifacts/qc.xlsx")
def download_qc(run_id: str) -> FileResponse:
    record = jobs.registry.get(run_id)
    if record is None or record.output_path is None or not record.output_path.exists():
        raise HTTPException(404, "QC workbook not available (run may have expired)")
    return FileResponse(
        path=str(record.output_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="File_For_Mapping_QC.xlsx",
    )


@app.get("/api/runs/{run_id}/artifacts/log.txt")
def download_log(run_id: str) -> FileResponse:
    record = jobs.registry.get(run_id)
    if record is None:
        raise HTTPException(404, "Run not found")

    # Path.write_text defaults to the locale encoding (cp1252 on Windows),
    # which can't encode pipeline glyphs like check / arrow / spinner chars.
    # Pin to UTF-8 so the whole log makes it to disk — analysts need the
    # complete Phase A + Phase B output to QC the cleaned workbook before
    # re-uploading.
    log_path = record.tmpdir / "run_log.txt"
    log_path.write_text("\n".join(record.log_lines), encoding="utf-8")
    return FileResponse(
        path=str(log_path),
        media_type="text/plain; charset=utf-8",
        filename="aic_run_log.txt",
    )


@app.get("/api/runs/{run_id}/artifacts/bundle.zip")
def download_bundle(run_id: str) -> FileResponse:
    """
    Archival bundle: every artifact and decision the analyst made on
    this run, packaged for record-keeping.

    Includes the primary output (xlsx) and the running log; if the run
    has progressed through QC the per-sheet edits are serialised; for
    Phase 2 runs the analyst's mismatch corrections are included; if a
    post-QC re-upload happened the per-category CSVs are nested under
    post_qc/.  metadata.json captures run identity so a downstream
    audit can match the bundle back to the run that produced it.
    """
    record = jobs.registry.get(run_id)
    if record is None:
        raise HTTPException(404, "Run not found")

    bundle_path = record.tmpdir / "bundle.zip"
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if record.output_path and record.output_path.exists():
            output_name = (
                "qc.xlsx" if record.phase == "phase1" else "output.xlsx"
            )
            zf.write(record.output_path, arcname=output_name)

        zf.writestr("log.txt", "\n".join(record.log_lines))

        if record.qc_edits:
            zf.writestr(
                "qc_edits.json",
                json.dumps(record.qc_edits, indent=2, sort_keys=True),
            )
        if record.mismatch_corrections:
            zf.writestr(
                "mismatch_corrections.json",
                json.dumps(record.mismatch_corrections, indent=2),
            )

        # Post-QC outputs are themselves a zip on disk; nest them under a
        # subdirectory so the bundle stays a single archive.
        if record.post_qc_zip_path and record.post_qc_zip_path.exists():
            with zipfile.ZipFile(record.post_qc_zip_path) as inner:
                for member in inner.namelist():
                    zf.writestr(f"post_qc/{member}", inner.read(member))

        zf.writestr("metadata.json", json.dumps({
            "run_id":        record.run_id,
            "phase":         record.phase,
            "state":         record.state,
            "started_at":    record.started_at,
            "finished_at":   record.finished_at,
            "parent_run_id": record.parent_run_id,
        }, indent=2))

    return FileResponse(
        path=str(bundle_path),
        media_type="application/zip",
        filename=f"aic_run_{run_id}.zip",
    )


# ── Phase 2 lifecycle ────────────────────────────────────────────────────────

@app.post("/api/phase2/runs", response_model=RunCreated)
async def create_phase2_run(
    zip:    UploadFile = File(...),
    config: str        = Form(...),     # JSON-serialised Phase2Config
) -> RunCreated:
    """
    Kick off a Phase 2 run from an uploaded zip + JSON config.

    The zip is extracted into the run's tmpdir; the worker scans the
    resulting directory for File_For_Mapping_QC.xlsx, ModelInfo.txt,
    Attributes.txt and AttributeValues.txt.
    """
    _require_zip(zip)
    cfg = _parse_phase2_config(config)

    tmpdir = Path(tempfile.mkdtemp(prefix="aic_p2_"))
    try:
        effective_dir = extract_input_zip(await zip.read(), tmpdir / "extracted")
    except zipfile.BadZipFile as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, f"Could not extract zip: {exc}")

    record = jobs.registry.create(phase="phase2", tmpdir=tmpdir)
    inputs = _phase2_inputs_from_config(cfg)
    worker.start_phase2(record, str(effective_dir), inputs)
    return RunCreated(run_id=record.run_id)


# ── Phase 2 input scan (autodetect columns + dropdown values) ───────────────
# Matches _load_cols_from_dir / _load_cols_from_bytes in the Streamlit page.
# Two routes: one for a full project zip, one for a single QC workbook.
#
# The scan extracts inputs into a tmpdir just long enough to read column
# metadata, then nukes the tmpdir before responding — the frontend keeps
# the metadata in memory and re-uploads the actual files when the user
# starts the run.  No bytes survive the request on disk.

_EMPTY_SCAN_ID = ""   # kept in the response model for API stability


@app.post("/api/phase2/scan", response_model=Phase2ScanResult)
async def scan_phase2_zip(zip: UploadFile = File(...)) -> Phase2ScanResult:
    _require_zip(zip)

    scan_dir = Path(tempfile.mkdtemp(prefix="aic_p2_scan_"))
    try:
        root = extract_zip_with_unwrap(await zip.read(), scan_dir / "extracted")
        meta = scan_phase2_directory(root)
    except InputError as exc:
        raise HTTPException(400, str(exc))
    finally:
        shutil.rmtree(scan_dir, ignore_errors=True)

    return Phase2ScanResult(scan_id=_EMPTY_SCAN_ID, **meta.__dict__)


@app.post("/api/phase2/scan/xlsx", response_model=Phase2ScanResult)
async def scan_phase2_xlsx_route(xlsx: UploadFile = File(...)) -> Phase2ScanResult:
    _require_xlsx(xlsx)

    scan_dir = Path(tempfile.mkdtemp(prefix="aic_p2_scan_"))
    target = scan_dir / xlsx.filename
    target.write_bytes(await xlsx.read())
    try:
        meta = scan_phase2_xlsx(target)
    except InputError as exc:
        raise HTTPException(400, str(exc))
    finally:
        shutil.rmtree(scan_dir, ignore_errors=True)

    return Phase2ScanResult(scan_id=_EMPTY_SCAN_ID, **meta.__dict__)


@app.get("/api/phase2/scan/from-parent/{parent_run_id}", response_model=Phase2ScanResult)
def scan_phase2_from_parent(parent_run_id: str) -> Phase2ScanResult:
    """
    Scan a parent Phase 1 run's QC workbook to populate Phase 2 dropdowns
    when arriving via the handoff flow (?parentRunId=…) — without making
    the user re-upload anything.
    """
    parent = jobs.registry.get(parent_run_id)
    if parent is None:
        raise HTTPException(404, "Parent run not found")
    qc_path = parent.tmpdir / "File_For_Mapping_QC.xlsx"
    if not qc_path.is_file():
        raise HTTPException(
            409,
            "Parent run has no QC workbook yet — finish Phase 1 QC first.",
        )
    try:
        meta = scan_phase2_xlsx(qc_path)
    except InputError as exc:
        raise HTTPException(400, str(exc))
    return Phase2ScanResult(scan_id=_EMPTY_SCAN_ID, **meta.__dict__)


# ── Phase 2 loose-files run ─────────────────────────────────────────────────
# Mirrors the 'Individual files' radio mode in pages/2_Phase_3_Pipeline_and_QC.py
# — analysts who finished Phase 1 in xlsx+csv mode and now need to run Phase 2
# don't have to repackage everything as a zip.

@app.post("/api/phase2/runs/files", response_model=RunCreated)
async def create_phase2_run_from_files(
    xlsx:             UploadFile = File(...),
    model_info:       UploadFile = File(...),
    attributes:       UploadFile = File(...),
    attribute_values: UploadFile = File(...),
    config:           str        = Form(...),
) -> RunCreated:
    _require_xlsx(xlsx)
    cfg = _parse_phase2_config(config)

    tmpdir = Path(tempfile.mkdtemp(prefix="aic_p2_"))
    project_dir = tmpdir / "extracted"
    project_dir.mkdir()

    (project_dir / "File_For_Mapping_QC.xlsx").write_bytes(await xlsx.read())
    (project_dir / "ModelInfo.txt").write_bytes(await model_info.read())
    (project_dir / "Attributes.txt").write_bytes(await attributes.read())
    (project_dir / "AttributeValues.txt").write_bytes(await attribute_values.read())

    record = jobs.registry.create(phase="phase2", tmpdir=tmpdir)
    inputs = _phase2_inputs_from_config(cfg)
    worker.start_phase2(record, str(project_dir), inputs)
    return RunCreated(run_id=record.run_id)


@app.post("/api/phase2/runs/from-parent/{parent_run_id}", response_model=RunCreated)
async def create_phase2_run_from_parent(
    parent_run_id: str,
    config:        str = Form(...),
) -> RunCreated:
    """
    Start Phase 2 from a finished Phase 1 run by copying its tmpdir
    contents (File_For_Mapping_QC.xlsx + any txt files extracted from
    the original zip) into a fresh tmpdir for the new run.

    Matches the Streamlit handoff path: when Phase 1 ran in zip mode,
    the same directory feeds Phase 2 without re-uploading.
    """
    parent = jobs.registry.get(parent_run_id)
    if parent is None:
        raise HTTPException(404, "Parent run not found")
    cfg = _parse_phase2_config(config)

    tmpdir = Path(tempfile.mkdtemp(prefix="aic_p2_"))
    project_dir = tmpdir / "extracted"
    project_dir.mkdir(parents=True, exist_ok=True)

    # Layout the worker needs at project_dir:
    #   File_For_Mapping_QC.xlsx — Phase 1 output (lives at parent.tmpdir/)
    #   ModelInfo.txt + Attributes.txt + AttributeValues.txt — the tool files
    #     extracted from the original zip into parent.tmpdir/extracted/, possibly
    #     with one or more wrapper folders inside.
    #
    # Previous version did `shutil.copytree(parent.tmpdir, project_dir)`, which
    # copied the parent's own `extracted/` subdir as a nested `extracted/extracted/`.
    # phase3_package.pipeline only scans one level deep, so the tool files ended
    # up hidden and Phase 2 raised FileNotFoundError on ModelInfo.txt.
    parent_extracted = parent.tmpdir / "extracted"
    copied: list[str] = []
    if parent_extracted.is_dir():
        visible = [e for e in parent_extracted.iterdir() if not e.name.startswith(".")]
        # Mirror extract_zip_with_unwrap: if the zip had a single wrapper folder,
        # use that as the effective root rather than parent_extracted itself.
        source_root = (
            visible[0] if len(visible) == 1 and visible[0].is_dir() else parent_extracted
        )
        for item in source_root.iterdir():
            target = project_dir / item.name
            if item.is_file():
                shutil.copy2(item, target)
                copied.append(item.name)
            elif item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
                copied.append(f"{item.name}/")

    # rglob fallback for required tool files: if the zip nested ModelInfo.txt
    # etc. deeper than a single wrapper folder, the loop above leaves them under
    # a sub-subdirectory.  Lift each one into project_dir/ so phase3_package's
    # one-level-deep scan can find it.  Tracked in `copied` for the audit log.
    #
    # Case-insensitive match: Linux is case-sensitive at the filesystem layer,
    # so a zip containing `modelinfo.txt` would slip past `rglob("ModelInfo.txt")`.
    # We always copy out under the canonical casing the worker expects.
    required = ("ModelInfo.txt", "Attributes.txt", "AttributeValues.txt")
    required_lower = {f.lower(): f for f in required}
    needed = {f for f in required if not (project_dir / f).is_file()}
    if needed:
        for candidate in parent.tmpdir.rglob("*"):
            if not candidate.is_file():
                continue
            canonical = required_lower.get(candidate.name.lower())
            if canonical is None or canonical not in needed:
                continue
            shutil.copy2(candidate, project_dir / canonical)
            try:
                rel = candidate.relative_to(parent.tmpdir)
            except ValueError:
                rel = candidate
            copied.append(f"{canonical} (rglob from {rel})")
            needed.discard(canonical)
            if not needed:
                break

    # Phase 1's qc_finalize wrote File_For_Mapping_QC.xlsx to parent.tmpdir/
    # (not into extracted/), so copy it explicitly onto the same level as the
    # tool files.  Done last so it overwrites any stale copy in the zip.
    parent_qc = parent.tmpdir / "File_For_Mapping_QC.xlsx"
    if parent_qc.is_file():
        shutil.copy2(parent_qc, project_dir / "File_For_Mapping_QC.xlsx")
        copied.append("File_For_Mapping_QC.xlsx")

    record = jobs.registry.create(
        phase="phase2", tmpdir=tmpdir, parent_run_id=parent.run_id,
    )
    # Audit trail for the handoff — kept to one line so the run log stays
    # focused on the actual pipeline output that follows.  If the handoff
    # ends up missing a tool file, _log_phase2_inputs in the worker will
    # dump the full layout for diagnosis at that point.
    n = len(copied)
    jobs.append_log(
        record,
        f"Phase 1 → Phase 2 handoff from run {parent.run_id} "
        f"({n} file{'s' if n != 1 else ''} copied)",
    )
    inputs = _phase2_inputs_from_config(cfg)
    worker.start_phase2(record, str(project_dir), inputs)
    return RunCreated(run_id=record.run_id)


def _phase2_inputs_from_config(cfg: Phase2Config) -> Phase2Inputs:
    pl_cfg = {k: v.model_dump() for k, v in cfg.private_label_config.items()}
    bo_cfg = cfg.brand_override_config.model_dump()
    return Phase2Inputs(
        raw_upc_pl_brand_col=cfg.raw_upc_pl_brand_col,
        private_label_config=pl_cfg,
        brand_override_config=bo_cfg,
        is_custom_collapse=cfg.is_custom_collapse,
        skip_rmrr=cfg.skip_rmrr,
    )


@app.get("/api/runs/{run_id}/mismatch", response_model=MismatchPayload)
def get_mismatch(run_id: str) -> MismatchPayload:
    record = jobs.registry.get(run_id)
    if record is None:
        raise HTTPException(404, "Run not found")
    if record.state != "mismatch_pending":
        raise HTTPException(
            409,
            f"Run is in state '{record.state}', no mismatch review available",
        )
    return MismatchPayload(
        groups=[MismatchGroup(**g) for g in record.mismatch_groups],
        brand_values=list(record.mismatch_brand_values),
        tool_brand_values=list(record.mismatch_tool_brand_values),
    )


@app.post("/api/runs/{run_id}/mismatch/resolve", response_model=Phase2Done)
def resolve_mismatch(run_id: str, payload: MismatchResolve) -> Phase2Done:
    """
    Submit analyst corrections.  The worker is parked on resume_event;
    setting it lets Phase B continue with the corrections applied.

    This call returns as soon as the worker has been signalled — the
    actual Phase B run is observed via /api/runs/{id}.  The download_url
    in the response will only become live once state == 'done'.
    """
    record = jobs.registry.get(run_id)
    if record is None:
        raise HTTPException(404, "Run not found")
    if record.state != "mismatch_pending":
        raise HTTPException(
            409, f"Run is in state '{record.state}', no mismatch review pending",
        )

    with record.lock:
        record.mismatch_corrections = [c.model_dump() for c in payload.corrections]
    record.resume_event.set()

    return Phase2Done(download_url=f"/api/runs/{run_id}/artifacts/output.xlsx")


@app.get("/api/runs/{run_id}/artifacts/output.xlsx")
def download_phase2_output(run_id: str) -> FileResponse:
    record = jobs.registry.get(run_id)
    if record is None or record.output_path is None or not record.output_path.exists():
        raise HTTPException(404, "Output workbook not available (run may have expired)")
    # URL path stays /artifacts/output.xlsx for routing stability; the
    # browser-visible save name comes from output_filename
    # (CATEGORY_DATE_qc_output.xlsx) so analysts get a self-identifying
    # file without renaming.
    return FileResponse(
        path=str(record.output_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=record.output_filename or "output.xlsx",
    )


# ── Post-QC re-upload (Phase 2/3 finalize → category-CSV zip) ───────────────

@app.post("/api/post_qc/standalone", response_model=RunCreated)
async def post_qc_standalone(
    xlsx: UploadFile = File(...),
    is_custom_collapse: str = Form("false"),
) -> RunCreated:
    """
    Standalone post-QC re-upload — does not require an existing run.

    The post-QC stage only needs the analyst-edited xlsx + a
    custom-collapse flag (see phase3_package.run_post_qc); none of the
    Phase 2 run's pickle/state is read.  A standalone entry point
    sidesteps two failure modes the run-scoped variant has:

      • the parent run was idle-evicted from the registry (60min TTL)
      • the BFF was restarted between Phase 2 finishing and the
        analyst getting back to upload (in-memory registry wipes)

    Either of those would 404 the run-scoped endpoint.  Standalone
    creates its own JobRecord, runs the worker, and returns the new
    run_id so the client can poll state and download the zip.
    """
    _require_xlsx(xlsx)

    tmpdir = Path(tempfile.mkdtemp(prefix="aic_postqc_"))
    edited_path = tmpdir / "output_edited.xlsx"
    edited_path.write_bytes(await xlsx.read())

    # phase="post_qc" (not "phase2") so the dashboard / ETA bucketing can
    # tell standalone re-uploads apart from a full Phase 2 run.  No
    # logic branches on phase today — _RECENT_DURATIONS only tracks
    # phase1 + phase2, so post_qc runs are silently excluded from ETA
    # projection (correct: they're seconds, not minutes).
    record = jobs.registry.create(phase="post_qc", tmpdir=tmpdir)
    custom_collapse = is_custom_collapse.strip().lower() in ("true", "1", "yes")
    worker.start_post_qc(record, str(edited_path), is_custom_collapse=custom_collapse)
    return RunCreated(run_id=record.run_id)


@app.post("/api/runs/{run_id}/post_qc", response_model=PostQcDone)
async def post_qc_re_upload(run_id: str, xlsx: UploadFile = File(...)) -> PostQcDone:
    """
    Accept an analyst-edited 'Cleaned Output' xlsx and start the post-QC
    worker.  Mirrors the upload-edited-output flow on the Streamlit page
    (lines 1416-1469): we save the file into the run's tmpdir, call
    run_post_qc to re-collapse + split by category, and bundle the
    resulting CSVs into a zip the user can download.

    The route returns as soon as the worker is started — actual progress
    is observed via /api/runs/{id} (state=post_qc_running → post_qc_done).
    """
    record = jobs.registry.get(run_id)
    if record is None:
        raise HTTPException(404, "Run not found")
    if record.state != "done":
        raise HTTPException(
            409,
            f"Run is in state '{record.state}', post-QC re-upload requires 'done'",
        )
    _require_xlsx(xlsx)

    edited_path = record.tmpdir / "output_edited.xlsx"
    edited_path.write_bytes(await xlsx.read())

    # is_custom_collapse is captured during Phase 2 config; for now we
    # default to False (matches the Streamlit toggle's default state).
    worker.start_post_qc(record, str(edited_path), is_custom_collapse=False)

    # Categories aren't known until the worker finishes; the response
    # populates them post-hoc by polling /api/runs/{id}.  We surface an
    # empty list immediately so the client can proceed.
    return PostQcDone(
        download_url=f"/api/runs/{run_id}/artifacts/post_qc.zip",
        categories=[],
    )


@app.get("/api/runs/{run_id}/artifacts/post_qc.zip")
def download_post_qc(run_id: str) -> FileResponse:
    record = jobs.registry.get(run_id)
    if (
        record is None
        or record.post_qc_zip_path is None
        or not record.post_qc_zip_path.exists()
    ):
        raise HTTPException(404, "Post-QC export not available (run may have expired)")
    return FileResponse(
        path=str(record.post_qc_zip_path),
        media_type="application/zip",
        filename="AIC_Phase2_3_exports.zip",
    )


# ── Static frontend ──────────────────────────────────────────────────────────
# Mount last so it doesn't shadow /api/*. In dev, web/out won't exist
# (you use `npm run dev` separately) and this is a no-op.
WEB_DIST = PROJECT_ROOT / "web" / "out"
if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
