#!/usr/bin/env python
# coding: utf-8
"""
Phase 1 — emergency fallback runner (no web app, no server).

Use this when the web app / HuggingFace Space is unavailable and an analyst needs
to run Phase 1 by hand (e.g. in the Jupyter cloud).  Point it at a folder (or a
.zip) containing the two Phase-1 inputs:

    * an Excel  "AttrGrid Final_<PROJECT>.xlsx"  with META and FINAL sheets, and
    * a flat-file CSV  "AttrGrid_<PROJECT>_<n>.csv".

It runs the EXACT production pipeline (Lookup -> BM25 || LinearSVC -> Ensemble)
and writes File_For_Mapping_QC.xlsx — the same QC workbook the app produces.
No internet required.

------------------------------------------------------------------------------
HOW TO RUN
------------------------------------------------------------------------------
This file lives in the repo's v2/ folder, next to the `api` and `ml_package`
packages it needs.  Make sure the whole v2/ folder (plus the requirements.txt
deps) is present in your environment, then:

  Command line:
      python phase1_fallback.py "<INPUT_FOLDER_OR_ZIP>" ["<OUTPUT_XLSX>"]

  Notebook / interactive:
      from phase1_fallback import run
      run(r"C:\\path\\to\\uploaded\\folder")        # writes alongside the inputs
      run(r"C:\\path\\to\\inputs.zip", r"C:\\out\\QC.xlsx")

If OUTPUT_XLSX is omitted, File_For_Mapping_QC.xlsx is written inside the input
folder (or the current directory for a .zip input).
"""
from __future__ import annotations

import sys
import time
import tempfile
from pathlib import Path

# ── Make the repo packages importable no matter where this is launched from ───
_REPO = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import api  # noqa: F401 — side-effect import primes NLTK corpora (walled-garden)
from api.inputs import find_phase1_inputs, extract_zip_with_unwrap
from api import pipeline


def _resolve_inputs(input_path: Path):
    """Return (excel_path, csv_path) for a folder OR a .zip input."""
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        dest = Path(tempfile.mkdtemp(prefix="p1_fallback_"))
        root = extract_zip_with_unwrap(input_path.read_bytes(), dest)
    elif input_path.is_dir():
        root = input_path
    else:
        raise SystemExit(
            f"Input is neither a folder nor a .zip: {input_path}\n"
            f"Point it at the folder you uploaded (with the AttrGrid Excel + CSV)."
        )
    return find_phase1_inputs(root)


def run(input_path: str, output_xlsx: str = "") -> Path:
    """Run Phase 1 on a folder/zip and write the QC workbook. Returns its path."""
    src = Path(input_path)
    xl, csv = _resolve_inputs(src)

    base_dir = src if src.is_dir() else Path.cwd()
    out = Path(output_xlsx) if output_xlsx else base_dir / "File_For_Mapping_QC.xlsx"

    print(f"  Excel : {xl.name}")
    print(f"  CSV   : {csv.name}")
    print(f"  Output: {out}")
    print("-" * 60)

    t0 = time.time()
    payload = pipeline.run_phase1(str(xl), str(csv))   # Lookup -> BM25||ML -> Ensemble
    sheets = list(payload.dictEnsemble.keys())

    if not sheets:
        print("-" * 60)
        print("WARNING: the pipeline matched 0 attributes / produced 0 QC sheets.")
        print("This is almost always a META/column mismatch: check that each META")
        print("'Attribute Name in MDM' value exists as a column in BOTH the FINAL")
        print("sheet and the flat-file CSV (watch for renamed/missing RAW_ columns).")
        return out

    pipeline.write_qc_excel(str(out), payload, {})
    size_mb = out.stat().st_size / (1024 * 1024)
    print("-" * 60)
    print(f"DONE in {time.time() - t0:.0f}s — {len(sheets)} QC sheet(s), {size_mb:.1f} MB")
    print(f"Sheets : {sheets}")
    print(f"Workbook written: {out}")
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(
            'Usage: python phase1_fallback.py "<INPUT_FOLDER_OR_ZIP>" ["<OUTPUT_XLSX>"]'
        )
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
