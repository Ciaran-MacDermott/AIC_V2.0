"""
Shared input-extraction helpers for Phase 1 + Phase 2 zip uploads.

Mirrors _extract_p1_zip in 1_Phase_1_Attribute_Mapping.py and _extract_zip
in 2_Phase_3_Pipeline_and_QC.py: extract the archive into a destination,
unwrap a single top-level wrapper folder, locate the per-phase inputs,
and (for Phase 2) autodetect column defaults so the new UI can mirror
Streamlit's pre-populated dropdowns.

Keeping this in one module means the route layer doesn't grow zip
plumbing and the error messages stay identical to what users saw in the
Streamlit UI.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional


# Priority lists mirror _UPC_PRIORITY / _MFR_PRIORITY in
# pages/2_Phase_3_Pipeline_and_QC.py — change in lock-step.
_UPC_PRIORITY = ["RAW_BRAND", "RAW_TRADEMARK", "RAW_SUB_BRAND", "RAW_US_TRADEMARK"]
_MFR_PRIORITY = ["RAW_MANUFACTURER", "RAW_PARENT", "RAW_US_PARENT"]
# Parent priority is distinct from manufacturer: the dialog and the
# private-label retailer detection both want the column whose values are
# retailer-shaped (e.g. "CVS PHARMACY"), not the manufacturer name.
_PARENT_PRIORITY = ["RAW_PARENT", "RAW_US_PARENT", "RAW_MANUFACTURER"]


class InputError(ValueError):
    """User-facing input validation error.  Surfaced as HTTP 400."""


def extract_zip_with_unwrap(zip_bytes: bytes, dest: Path) -> Path:
    """
    Extract ``zip_bytes`` into ``dest`` and return the effective root.

    If the archive contains a single visible top-level directory, that
    directory is the working root — matching how analysts package
    multi-file project inputs.
    """
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            zf.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise InputError(f"Could not extract zip: {exc}") from exc

    visible = [e for e in dest.iterdir() if not e.name.startswith(".")]
    if len(visible) == 1 and visible[0].is_dir():
        return visible[0]
    return dest


def find_phase1_inputs(root: Path) -> tuple[Path, Path]:
    """
    Locate the Excel (META + FINAL) and CSV inside an extracted zip.

    Skips File_For_Mapping_QC.xlsx so a Phase 1 + Phase 2 combined zip
    doesn't accidentally try to use the post-Phase-1 output as input.
    """
    try:
        import openpyxl  # local import — heavy dep, not always present in fast tests
    except ImportError as exc:
        raise InputError(
            "openpyxl is required to validate Phase 1 zip uploads",
        ) from exc

    xl_path: Optional[Path] = None
    for p in sorted(root.rglob("*.xlsx")):
        if "file_for_mapping_qc" in p.name.lower():
            continue
        try:
            wb = openpyxl.load_workbook(str(p), read_only=True)
            names_upper = [s.upper() for s in wb.sheetnames]
            wb.close()
        except Exception:
            continue
        if any("META" in n for n in names_upper) and any(
            "FINAL" in n for n in names_upper
        ):
            xl_path = p
            break

    if xl_path is None:
        raise InputError(
            "No Excel file with META and FINAL sheets found in the ZIP. "
            "Check the file is included and the sheet names are correct.",
        )

    csv_path: Optional[Path] = None
    for p in sorted(root.rglob("*.csv")):
        csv_path = p
        break

    if csv_path is None:
        raise InputError("No .csv flat file found in the ZIP.")

    return xl_path, csv_path


# ── Phase 2 column scanning ─────────────────────────────────────────────────

@dataclass
class Phase2Scan:
    raw_upc_columns:           list[str]
    raw_manufacturer_columns:  list[str]
    raw_parent_columns:        list[str]
    all_columns:               list[str]
    default_upc_col:           str
    default_manufacturer_col:  str
    default_parent_col:        str
    manufacturer_values:       list[str]
    brand_values:              list[str]
    tool_brand_values:         list[str]
    # Distinct values per column (bounded by the same 500-row read used
    # for the rest of this scan).  Lets the brand-override rule editor
    # populate its dropdowns from whichever columns the analyst picked
    # in the column-name fields above, instead of being hard-coded to
    # the BRAND / TOOL_BRAND / default-manufacturer columns.
    column_values:             dict[str, list[str]]


def scan_phase2_xlsx(xlsx_path: Path) -> Phase2Scan:
    """
    Inspect File_For_Mapping_QC.xlsx and return the autodetected
    column metadata.  Mirrors _load_cols_from_dir / _load_cols_from_bytes
    in the Streamlit page so the new UI can pre-fill the same defaults
    without the user typing column names by hand.

    Raises ``InputError`` when the workbook has no FLAT_FILE sheet.
    """
    try:
        import pandas as pd  # local — pandas isn't always installed in stub envs
    except ImportError as exc:
        raise InputError(
            "pandas is required to scan Phase 2 input workbooks",
        ) from exc

    try:
        xf = pd.ExcelFile(str(xlsx_path))
    except Exception as exc:
        raise InputError(f"Could not open workbook: {exc}") from exc

    sheet_map = {s.upper(): s for s in xf.sheet_names}
    flat = sheet_map.get("FLAT_FILE")
    if not flat:
        raise InputError(
            "FLAT_FILE sheet not found in workbook — Phase 2 scan needs the "
            "post-Phase-1 QC workbook with its FLAT_FILE sheet intact.",
        )

    df = pd.read_excel(str(xlsx_path), sheet_name=flat, nrows=500)
    all_cols = [c for c in df.columns if not str(c).startswith("Unnamed")]
    raw_cols = [c for c in all_cols if str(c).upper().startswith("RAW")]

    raw_upper = {c.upper(): c for c in raw_cols}
    default_upc = next(
        (raw_upper[u] for u in _UPC_PRIORITY if u in raw_upper),
        raw_cols[0] if raw_cols else "",
    )
    default_mfr = next(
        (raw_upper[m] for m in _MFR_PRIORITY if m in raw_upper),
        raw_cols[0] if raw_cols else "",
    )
    default_parent = next(
        (raw_upper[m] for m in _PARENT_PRIORITY if m in raw_upper),
        raw_cols[0] if raw_cols else "",
    )

    def _uniq(col: str) -> list[str]:
        if col in df.columns:
            return sorted(
                df[col].dropna().astype(str).str.strip().loc[lambda s: s != ""].unique().tolist()
            )
        return []

    column_values = {col: _uniq(col) for col in all_cols}

    return Phase2Scan(
        raw_upc_columns=raw_cols,
        raw_manufacturer_columns=raw_cols,   # same set; UI surfaces both
        raw_parent_columns=raw_cols,         # parent picker draws from the same RAW_* set
        all_columns=all_cols,
        default_upc_col=default_upc,
        default_manufacturer_col=default_mfr,
        default_parent_col=default_parent,
        manufacturer_values=_uniq(default_mfr),
        brand_values=_uniq("BRAND"),
        tool_brand_values=_uniq("TOOL_BRAND"),
        column_values=column_values,
    )


def scan_phase2_directory(root: Path) -> Phase2Scan:
    """
    Find File_For_Mapping_QC.xlsx anywhere under ``root`` and scan it.
    Streamlit walks subdirectories because zips often have a wrapper.
    """
    for p in sorted(root.rglob("*.xlsx")):
        if "file_for_mapping_qc" not in p.name.lower():
            continue
        return scan_phase2_xlsx(p)
    raise InputError(
        "File_For_Mapping_QC.xlsx not found in the ZIP — make sure your "
        "Phase 1 output is included.",
    )
