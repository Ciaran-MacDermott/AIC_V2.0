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
# PL detection + the BRAND-vs-TOOL_BRAND mismatch dialog want the column
# whose values look like retailers (e.g. "CVS PHARMACY"). In current
# Circana data that's RAW_MANUFACTURER; RAW_PARENT / RAW_US_PARENT are
# fallbacks for older project shapes.
_PARENT_PRIORITY = ["RAW_MANUFACTURER", "RAW_PARENT", "RAW_US_PARENT"]


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
    # Literal (brand_col, tool_brand_col) pairs resolved from each
    # Attributes.txt's Brand_Attribute=Y row.  Empty when the project has
    # no Attributes.txt (loose-file mode) or no Brand_Attribute column
    # (legacy data).  Mirrored 1:1 to Phase2ScanResult.detected_brand_pairs.
    detected_brand_pairs:      list[dict[str, str]]


def _resolve_brand_pairs_from_dir(root: Path) -> list[tuple[str, str]]:
    """
    Walk ``root`` recursively for every Attributes.txt and collect
    (brand_col, tool_brand_col) pairs from Brand_Attribute=Y rows. Empty
    list when no Attributes.txt is present or no Brand_Attribute column
    exists — callers fall back to literal BRAND / TOOL_BRAND columns in
    that case.

    Recursive walk handles nested layouts (project/Tool_files/Sub/Attributes.txt)
    in addition to flat (root/Attributes.txt) and 1-level-deep
    (root/Tool_files_X/Attributes.txt) ones.
    """
    try:
        import pandas as pd  # local
    except ImportError:
        return []

    if not root.is_dir():
        return []
    attribute_files = sorted(root.rglob("Attributes.txt"))

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in attribute_files:
        try:
            attrs = pd.read_csv(str(path), delimiter="|")
        except Exception:
            continue
        if "Brand_Attribute" not in attrs.columns or "Attribute_Name" not in attrs.columns:
            continue
        flagged = attrs[
            attrs["Brand_Attribute"].astype(str).str.strip().str.upper() == "Y"
        ]
        for raw_name in flagged["Attribute_Name"].dropna():
            tool_name = str(raw_name).strip()
            if not tool_name.upper().startswith("TOOL_"):
                continue
            brand_name = tool_name[len("TOOL_"):]
            if not brand_name:
                continue
            key = (brand_name, tool_name)
            if key not in seen:
                seen.add(key)
                pairs.append(key)
    return pairs


def scan_phase2_xlsx(xlsx_path: Path, brand_pairs: Optional[list] = None) -> Phase2Scan:
    """
    Inspect File_For_Mapping_QC.xlsx and return autodetected column metadata.
    ``brand_pairs`` (from a sibling Attributes.txt walk) drives the
    brand_values / tool_brand_values surfaces; when empty, falls back to
    literal BRAND / TOOL_BRAND columns. Raises ``InputError`` when the
    FLAT_FILE sheet is missing.
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

    col_upper_map = {str(c).upper(): c for c in df.columns}

    def _uniq(col: str) -> list[str]:
        if col in df.columns:
            return sorted(
                df[col].dropna().astype(str).str.strip().loc[lambda s: s != ""].unique().tolist()
            )
        return []

    def _uniq_union(col_names: list[str]) -> list[str]:
        merged: set[str] = set()
        for name in col_names:
            actual = col_upper_map.get(str(name).upper())
            if actual is None:
                continue
            merged.update(_uniq(actual))
        return sorted(merged)

    column_values = {col: _uniq(col) for col in all_cols}

    # Brand / tool_brand values: pulled from every column the brand_pairs
    # resolve to (multi-model projects can declare different bases per
    # model — union the values so a single rules list works across all).
    if brand_pairs:
        brand_cols  = [b for b, _ in brand_pairs]
        tool_cols   = [t for _, t in brand_pairs]
        brand_vals  = _uniq_union(brand_cols)
        tool_vals   = _uniq_union(tool_cols)
        pair_dicts  = [{"brand_col": b, "tool_brand_col": t} for b, t in brand_pairs]
    else:
        brand_vals  = _uniq("BRAND")
        tool_vals   = _uniq("TOOL_BRAND")
        pair_dicts  = []

    return Phase2Scan(
        raw_upc_columns=raw_cols,
        raw_manufacturer_columns=raw_cols,   # same set; UI surfaces both
        raw_parent_columns=raw_cols,         # parent picker draws from the same RAW_* set
        all_columns=all_cols,
        default_upc_col=default_upc,
        default_manufacturer_col=default_mfr,
        default_parent_col=default_parent,
        manufacturer_values=_uniq(default_mfr),
        brand_values=brand_vals,
        tool_brand_values=tool_vals,
        column_values=column_values,
        detected_brand_pairs=pair_dicts,
    )


def scan_phase2_directory(root: Path) -> Phase2Scan:
    """
    Find File_For_Mapping_QC.xlsx under ``root`` and scan it. Resolves brand
    pairs from sibling Attributes.txt files for multi-model and custom-brand
    projects (handles BRAND_MULO, SUB_BRAND, etc.).
    """
    for p in sorted(root.rglob("*.xlsx")):
        if "file_for_mapping_qc" not in p.name.lower():
            continue
        brand_pairs = _resolve_brand_pairs_from_dir(root)
        return scan_phase2_xlsx(p, brand_pairs=brand_pairs)
    raise InputError(
        "File_For_Mapping_QC.xlsx not found in the ZIP — make sure your "
        "Phase 1 output is included.",
    )
