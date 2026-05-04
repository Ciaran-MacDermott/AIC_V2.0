"""
Phase 1 pipeline adapter.

This file is the only place that knows about ml_package. It mirrors the
orchestration in 1_Phase_1_Attribute_Mapping.py::_run_pipeline so outputs
stay reproducible whether the user runs Streamlit (legacy) or the new
FastAPI/Next.js UI.

The worker thread captures stdout into the JobRecord's log buffer, so
all the print() statements in this function flow into the live log box
exactly the way they do in the Streamlit version.

Stage progression mirrors _STAGE_PROGRESS in the Streamlit page.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

import openpyxl
import pandas as pd

from ml_package import ensemble as _ens
from ml_package import mapping_lookup as _ml
from ml_package import text_match as _tm
from ml_package import xgb_classifier as _rfx
from ml_package.write_results import write_results


STAGE_PROGRESS = {
    "read inputs":   0.10,
    "mappinglookup": 0.22,
    "textmatch":     0.68,
    "ensemble":      0.78,
    "qc_ready":      0.85,
    "write output":  0.98,
    "done":          1.00,
}

STAGE_LABELS = {
    "read inputs":   "Reading & validating input files…",
    "mappinglookup": "Running exact & fuzzy lookup matching…",
    "textmatch":     "Running BM25 + XGBoost in parallel (ML corroboration)…",
    "ensemble":      "Combining lookup, BM25 and ML scores via ensemble…",
    "qc_ready":      "Pipeline complete — QC review ready",
    "write output":  "Writing output Excel workbook…",
    "done":          "Done",
}


class PipelineStopped(Exception):
    """Raised between stages when the user has requested a stop."""


@dataclass
class Phase1Payload:
    FINAL: pd.DataFrame
    FLAT_FILE_OUT: Any
    meta: pd.DataFrame
    dictEnsemble: dict[str, pd.DataFrame]


def run_phase1(excel_path: str, csv_path: str,
               stop_event: Optional[threading.Event] = None) -> Phase1Payload:
    """
    Mirror of streamlit_app `_run_pipeline` minus the stdout redirection
    plumbing — the worker handles that.
    """
    def _maybe_stop():
        if stop_event and stop_event.is_set():
            raise PipelineStopped()

    # ── Read inputs ───────────────────────────────────────────────────────
    print("START read inputs")
    print(f"  Reading Excel: {os.path.basename(excel_path)}")
    wb          = openpyxl.load_workbook(excel_path, read_only=True)
    sheet_names = wb.sheetnames
    wb.close()
    print(f"  Excel sheets found: {sheet_names}")

    meta_sheet  = next((s for s in sheet_names if "META"  in s.upper()), None)
    final_sheet = next((s for s in sheet_names if "FINAL" in s.upper()), None)
    if not meta_sheet:
        raise RuntimeError(f"No META sheet in {excel_path}. Found: {sheet_names}")
    if not final_sheet:
        raise RuntimeError(f"No FINAL sheet in {excel_path}. Found: {sheet_names}")

    print(f"  Loading META sheet ({meta_sheet}) and FINAL sheet ({final_sheet})…")
    metaGridPDF    = pd.read_excel(excel_path, sheet_name=meta_sheet)
    combinedAMPPDF = pd.read_excel(excel_path, sheet_name=final_sheet)
    print(f"  Reading CSV: {os.path.basename(csv_path)}")
    attrGridPDF    = pd.read_csv(csv_path, low_memory=False)
    print(f"  Loaded — META {metaGridPDF.shape} | FINAL {combinedAMPPDF.shape} | CSV {attrGridPDF.shape}")

    FINAL   = combinedAMPPDF.copy()
    SAL_COL = "RAW_TOTAL_DOLLARS"
    if SAL_COL not in combinedAMPPDF.columns:
        SAL_COL = next((c for c in combinedAMPPDF.columns if "dollar" in c.lower()), None)
        print(f"  WARNING: using {SAL_COL!r} as sales column")
    print("DONE read inputs")
    _maybe_stop()

    # ── Validate + prep META ──────────────────────────────────────────────
    for col in ("Attribute Name in MDM", "Attribute Group name", "Attribute_Type", "Type"):
        if col not in metaGridPDF.columns:
            raise RuntimeError(f"META sheet missing column: '{col}'")

    metaGridPDF_old = metaGridPDF.copy()
    metaGridPDF     = metaGridPDF[metaGridPDF["Attribute_Type"] == "MODELING"]
    metaFields      = list(set(
        list(metaGridPDF["Attribute Name in MDM"].unique()) +
        list(metaGridPDF["Attribute Group name"].unique())
    ))

    missing_flat = list(set(metaGridPDF["Attribute Name in MDM"]) - set(attrGridPDF.columns))
    missing_amp  = list(set(metaFields) - set(combinedAMPPDF.columns))
    if missing_flat:
        print(f"  WARNING - columns missing from CSV: {missing_flat}")
    if missing_amp:
        print(f"  WARNING - columns missing from FINAL: {missing_amp}")

    attrGridPDF    = attrGridPDF.astype(object).fillna("nan").astype(str)
    combinedAMPPDF = combinedAMPPDF.astype(object).fillna("nan").astype(str)
    metaGridPDF    = metaGridPDF.astype(object).fillna("nan").astype(str)
    combinedAMPPDF["TOTAL_UNIT_SALES"] = pd.to_numeric(
        combinedAMPPDF[SAL_COL], errors="coerce"
    ).fillna(0)

    dictRecom: dict = {}

    # ── Lookup ────────────────────────────────────────────────────────────
    print("START mappinglookup")
    print("  Exact & fuzzy lookup — finds direct matches for new products using values already seen in the historical labelled data.")
    n_attrs = len(metaGridPDF["Attribute Name in MDM"].unique())
    n_new   = len(attrGridPDF)
    print(f"  Matching {n_new} new products against {n_attrs} attributes…")
    dictRecom, _, FLAT_FILE_OUT, dict_split_parent = _ml.runLookup(
        attrGridPDF, metaGridPDF, combinedAMPPDF, dictRecom
    )
    print("DONE mappinglookup")
    _maybe_stop()

    # ── BM25 + XGBoost in parallel ────────────────────────────────────────
    print("START textmatch")
    print("  BM25 + XGBoost — for products not resolved by lookup, BM25 ranks candidates by keyword relevance while XGBoost applies a tree-based classifier trained on historical labels. Both run in parallel.")
    _dictRecom_tm: dict = {}
    _dictRecom_ml: list = [None]

    def _run_tm():
        if stop_event and stop_event.is_set():
            return
        _tm.runTextMatch(metaGridPDF, combinedAMPPDF, attrGridPDF, _dictRecom_tm)

    def _run_ml():
        if stop_event and stop_event.is_set():
            return
        _dictRecom_ml[0] = _rfx.runML(attrGridPDF, metaGridPDF, combinedAMPPDF)

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_tm = pool.submit(_run_tm)
        f_ml = pool.submit(_run_ml)
        f_tm.result()
        f_ml.result()

    dictRecom.update(_dictRecom_tm)
    if _dictRecom_ml[0]:
        dictRecom.update(_dictRecom_ml[0])
    print("DONE textmatch")
    _maybe_stop()

    # ── Ensemble ──────────────────────────────────────────────────────────
    print("START ensemble")
    print("  Combining ML predictions with lookup results and assigning QC priority…")
    dictEnsemble = _ens.runEnsemble(dictRecom, metaGridPDF, dict_split_parent)
    print("DONE ensemble")
    _maybe_stop()

    print(f"DONE pipeline — {len(dictEnsemble)} lookup sheet(s) ready for QC review: {', '.join(dictEnsemble.keys())}")
    return Phase1Payload(
        FINAL=FINAL,
        FLAT_FILE_OUT=FLAT_FILE_OUT,
        meta=metaGridPDF_old,
        dictEnsemble=dictEnsemble,
    )


def write_qc_excel(out_path: str, payload: Phase1Payload,
                   qc_edits: dict[str, pd.DataFrame]) -> None:
    """
    Write File_For_Mapping_QC.xlsx using analyst-edited lookup DataFrames.

    Sheets present in `qc_edits` override the originals from the pipeline;
    any key not in qc_edits falls back to payload.dictEnsemble[key].
    """
    final_dict = {
        key: qc_edits.get(key, payload.dictEnsemble[key])
        for key in payload.dictEnsemble.keys()
    }
    write_results(out_path, payload.FINAL, payload.FLAT_FILE_OUT, payload.meta, final_dict)
