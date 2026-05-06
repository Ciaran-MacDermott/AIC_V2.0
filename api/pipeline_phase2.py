"""
Phase 2 + 3 pipeline adapter.

Mirrors the orchestration in pages/2_Phase_3_Pipeline_and_QC.py minus the
Streamlit plumbing.  The legacy code already cleanly separates:

  Phase A : run_through_step_12()  →  may surface BRAND/TOOL_BRAND mismatches
  Pause   : analyst applies corrections  (only if mismatches exist)
  Phase B : run_from_step_14()     →  writes the cleaned output workbook

The adapter is split the same way so the worker can park between A and B
on a threading.Event while the user reviews mismatches in the browser.

Stage progression mirrors the Streamlit step indicator so the front-end
progress bar lines up with the pipeline's actual position.
"""

from __future__ import annotations

import re
import threading
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from phase3_package.pipeline import (
    run_from_step_14,
    run_through_step_12,
)


# ═══════════════════════════════════════════════════════════════════════════
# Stage progression
# ═══════════════════════════════════════════════════════════════════════════
# Each key is a substring matched against printed stage banners; the value
# is the progress fraction the bar should reach when the worker observes
# that line.  Keep keys lowercase — _LogStream lowercases before matching.
STAGE_PROGRESS_PHASE2 = {
    "phase 2 aic processing":               0.20,
    "phase 3 quality checks":               0.45,
    "awaiting user review":                 0.55,   # mismatch pause
    "skus prepared and collapsed":          0.85,
    "attribute qc (tool vs mdm comparison)": 0.95,
    "done":                                  1.00,
}

STAGE_LABELS_PHASE2 = {
    "phase 2 aic processing":               "Phase 2 — Attribute assembly…",
    "phase 3 quality checks":               "Phase 3 — Quality checks & transformations…",
    "awaiting user review":                 "Awaiting mismatch review…",
    "skus prepared and collapsed":          "Phase 3 — SKU collapse…",
    "attribute qc (tool vs mdm comparison)": "Phase 3 — Attribute QC…",
    "done":                                  "Done",
}


# ═══════════════════════════════════════════════════════════════════════════
# Sentinels
# ═══════════════════════════════════════════════════════════════════════════
class PipelineStopped(Exception):
    """Raised when the user has requested a stop."""


class MismatchReviewNeeded(Exception):
    """
    Raised by run_phase_a when Phase A produced one or more BRAND vs
    TOOL_BRAND mismatch groups that need analyst review before Phase B
    can run.  Carries the groups so the worker can attach them to the
    JobRecord and park on resume_event.
    """

    def __init__(self, groups: list[dict[str, Any]],
                 phase_a_state: "Phase2InterimState"):
        super().__init__(f"{len(groups)} mismatch group(s) need review")
        self.groups = groups
        self.phase_a_state = phase_a_state


# ═══════════════════════════════════════════════════════════════════════════
# Default configuration
# ═══════════════════════════════════════════════════════════════════════════
# Mirrors _DEFAULT_PL_ROWS / _build_pl_config in the Streamlit page so the
# new UI can ship without re-implementing every option on day one.

DEFAULT_PRIVATE_LABEL_CONFIG: dict[str, Any] = {
    "walmart": {"enabled": True,  "label": "PRIVATE LABEL RESTRICTED"},
    "cvs":     {"enabled": True,  "label": "PRIVATE LABEL EXCLUDE"},
    "heb":     {"enabled": False, "label": "PRIVATE LABEL RESTRICTED"},
}

DEFAULT_BRAND_OVERRIDE_CONFIG: dict[str, Any] = {
    "enable":               False,
    "raw_manufacturer_col": "RAW_MANUFACTURER",
    "raw_parent_col":       "RAW_PARENT",
    "rules":                [],
}


@dataclass
class Phase2Inputs:
    """Configuration captured from the request and frozen for the worker."""
    raw_upc_pl_brand_col:  str
    private_label_config:  dict[str, Any]
    brand_override_config: dict[str, Any]
    is_custom_collapse:    bool = False
    skip_rmrr:             bool = False


@dataclass
class Phase2InterimState:
    """
    Output of Phase A that needs to survive across the mismatch-review
    pause so Phase B can pick up exactly where A left off.
    """
    df:                pd.DataFrame
    duplicate_dimkeys: pd.DataFrame
    pipeline_context:  dict[str, Any]


@dataclass
class Phase2Result:
    """Final pipeline output after Phase B completes."""
    collapsed_df:      pd.DataFrame
    duplicate_dimkeys: pd.DataFrame
    output_xlsx_path:  Path
    # Friendly name surfaced to the browser via Content-Disposition.  The
    # on-disk filename stays "output.xlsx" so existing routes / tests / the
    # post-QC re-upload pipeline keep working unchanged.
    output_filename:   str = "output.xlsx"


# ═══════════════════════════════════════════════════════════════════════════
# Zip extraction
# ═══════════════════════════════════════════════════════════════════════════

def extract_input_zip(zip_bytes: bytes, dest: Path) -> Path:
    """
    Unpack the uploaded zip into ``dest`` and resolve its effective root.

    Mirrors _extract_zip in the Streamlit page: if the archive contains a
    single top-level folder, that folder is the working directory.  This
    matches how analysts package multi-model project inputs.

    Returns the absolute path to the directory that holds the data files.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        zf.extractall(dest)

    entries = [e for e in dest.iterdir() if not e.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


# ═══════════════════════════════════════════════════════════════════════════
# Phase A
# ═══════════════════════════════════════════════════════════════════════════

def run_phase_a(directory_path: Path, inputs: Phase2Inputs,
                stop_event: Optional[threading.Event] = None
                ) -> Phase2InterimState:
    """
    Run Steps 1–12 plus mismatch detection.

    If any BRAND vs TOOL_BRAND mismatches surface, raise MismatchReviewNeeded
    so the worker can park on its resume_event while the analyst reviews.
    Otherwise return Phase2InterimState ready to feed into Phase B.
    """
    if stop_event and stop_event.is_set():
        raise PipelineStopped()

    df, dup_df, mismatch_groups, ctx = run_through_step_12(
        directory_path=str(directory_path),
        raw_upc_pl_brand_col=inputs.raw_upc_pl_brand_col,
        private_label_config=inputs.private_label_config,
        brand_override_config=inputs.brand_override_config,
        is_custom_collapse=inputs.is_custom_collapse,
        skip_rmrr=inputs.skip_rmrr,
    )

    state = Phase2InterimState(
        df=df,
        duplicate_dimkeys=dup_df,
        pipeline_context=ctx,
    )

    if mismatch_groups:
        # Worker will pause on resume_event until the analyst submits
        # corrections via /api/runs/{id}/mismatch/resolve.
        raise MismatchReviewNeeded(groups=mismatch_groups, phase_a_state=state)

    return state


# ═══════════════════════════════════════════════════════════════════════════
# Phase B
# ═══════════════════════════════════════════════════════════════════════════

def run_phase_b(state: Phase2InterimState,
                corrections: Optional[list[dict[str, Any]]],
                output_dir: Path,
                stop_event: Optional[threading.Event] = None
                ) -> Phase2Result:
    """
    Apply analyst corrections (if any), run Steps 14-17, then write the
    cleaned-output workbook to ``output_dir / output.xlsx``.

    The output sheet names mirror the Streamlit version exactly so any
    downstream tooling (post-QC importer, report templates) keeps working.
    """
    if stop_event and stop_event.is_set():
        raise PipelineStopped()

    collapsed_df, dup_df = run_from_step_14(
        df=state.df,
        pipeline_context=state.pipeline_context,
        corrections=corrections,
    )

    modeling_cols = _modeling_attribute_columns(
        state.pipeline_context.get("meta_df")
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_xlsx = output_dir / "output.xlsx"
    with pd.ExcelWriter(output_xlsx, engine="xlsxwriter") as writer:
        collapsed_df.to_excel(writer, sheet_name="Cleaned Output", index=False)
        _format_sheet(writer, collapsed_df, "Cleaned Output", modeling_cols)
        if dup_df is not None and not dup_df.empty:
            dup_df.to_excel(writer, sheet_name="Duplicate Keys", index=False)
            _format_sheet(writer, dup_df, "Duplicate Keys", modeling_cols=())

    return Phase2Result(
        collapsed_df=collapsed_df,
        duplicate_dimkeys=dup_df,
        output_xlsx_path=output_xlsx,
        output_filename=_derive_output_filename(collapsed_df),
    )


# ── Output formatting helpers ────────────────────────────────────────────────
#
# The cleaned-output workbook is what analysts actually open in Excel for
# post-pipeline QC, so a few small affordances dramatically streamline that
# review:
#   • Friendly filename derived from category + date so saved copies are
#     self-identifying without renaming.
#   • Grey, bold header row with a frozen top pane and an autofilter.
#   • Per-column widths sized to content (capped) so cells aren't truncated
#     to ``####`` or three letters wide.
#   • Light-purple tint on MODELING attribute columns so the analyst can
#     instantly distinguish modeling outputs from REPORTING / RAW / ID
#     passthrough columns.

# Cap column widths so a single 500-char DESCRIPTION row doesn't blow up
# the worksheet layout.
_COL_WIDTH_CAP = 60

_HEADER_FILL          = "#D9D9D9"
_HEADER_MODELING_FILL = "#C9B5DC"   # noticeably more purple than data tint
_MODELING_CELL_FILL   = "#EEE6F5"   # very light purple — readable behind text


def _modeling_attribute_columns(meta_df: Optional[pd.DataFrame]) -> list[str]:
    """
    Return the output columns whose META Attribute_Type is exclusively
    MODELING.  Mirrors the classification logic in aic_phase2.aic_code so
    the workbook tint matches what the pipeline treated as MODELING.
    """
    if meta_df is None or meta_df.empty:
        return []
    if "Attribute Group name" not in meta_df.columns or "Attribute_Type" not in meta_df.columns:
        return []
    out: list[str] = []
    for grp_name, grp in meta_df.groupby("Attribute Group name"):
        types = grp["Attribute_Type"].dropna().unique().tolist()
        if types == ["MODELING"]:
            out.append(str(grp_name))
    return out


def _derive_output_filename(df: pd.DataFrame) -> str:
    """
    Build ``CATEGORY_YYYY-MM-DD_qc_output.xlsx`` from the cleaned-output
    dataframe.  The category is sourced from ASSORTMENT_CATEGORY_DEFINITION,
    which Step 2 of the pipeline aligns to ModelInfo's Category_Name — so
    every row carries the same value by the time we get here.

    Falls back to ``OUTPUT_<date>_qc_output.xlsx`` if the column is missing
    or all-blank, keeping the download path safe.
    """
    cat_col = next(
        (c for c in df.columns if str(c).upper() == "ASSORTMENT_CATEGORY_DEFINITION"),
        None,
    )
    cat_raw = ""
    if cat_col is not None and len(df):
        non_null = df[cat_col].dropna()
        if not non_null.empty:
            cat_raw = str(non_null.iloc[0]).strip()

    # Sanitise for filesystem (and for URL-encoding sanity in Content-
    # Disposition) — strip anything that isn't alphanumeric, hyphen, or
    # underscore.
    cat = re.sub(r"[^A-Za-z0-9]+", "_", cat_raw).strip("_")
    today = date.today().isoformat()
    if cat:
        return f"{cat}_{today}_qc_output.xlsx"

    # Fallback: no category column / all-blank values.  Append a short
    # uuid suffix so two analysts hitting this branch on the same day
    # don't get colliding "OUTPUT_2026-05-05_qc_output.xlsx" filenames
    # in their downloads folder.
    suffix = uuid.uuid4().hex[:4]
    return f"OUTPUT_{today}_{suffix}_qc_output.xlsx"


def _format_sheet(writer: "pd.ExcelWriter", df: pd.DataFrame,
                  sheet_name: str, modeling_cols: tuple[str, ...] | list[str] = ()
                  ) -> None:
    """
    Apply the standard Cleaned Output layout to ``sheet_name``: grey
    header, sized columns, autofilter, frozen top row, plus light-purple
    tint on any column whose name appears in ``modeling_cols``.
    """
    workbook = writer.book
    worksheet = writer.sheets[sheet_name]

    header_fmt = workbook.add_format({
        "bg_color": _HEADER_FILL,
        "bold":     True,
        "border":   1,
        "align":    "left",
        "valign":   "vcenter",
    })
    header_modeling_fmt = workbook.add_format({
        "bg_color": _HEADER_MODELING_FILL,
        "bold":     True,
        "border":   1,
        "align":    "left",
        "valign":   "vcenter",
    })
    modeling_cell_fmt = workbook.add_format({"bg_color": _MODELING_CELL_FILL})

    modeling_set = {str(c).strip().upper() for c in modeling_cols}
    n_rows = len(df)
    n_cols = len(df.columns)

    for col_idx, col_name in enumerate(df.columns):
        is_modeling = str(col_name).strip().upper() in modeling_set

        # Column width sized to max(header, longest cell) + 2, capped.
        if n_rows:
            col_data = df[col_name].astype(str)
            max_content = int(col_data.str.len().max() or 0)
        else:
            max_content = 0
        width = min(max(len(str(col_name)), max_content) + 2, _COL_WIDTH_CAP)

        # set_column's optional format applies to data cells in this column
        # that don't have an explicit cell-level format.  pandas' to_excel
        # writes raw values without per-cell formats, so the modeling tint
        # takes effect here.  The header cell gets overwritten below.
        if is_modeling:
            worksheet.set_column(col_idx, col_idx, width, modeling_cell_fmt)
        else:
            worksheet.set_column(col_idx, col_idx, width)

        worksheet.write(
            0, col_idx, str(col_name),
            header_modeling_fmt if is_modeling else header_fmt,
        )

    if n_rows > 0 and n_cols > 0:
        # Autofilter spans header (row 0) through the last data row.
        worksheet.autofilter(0, 0, n_rows, n_cols - 1)
        # Freeze the header so it stays visible while the analyst scrolls.
        worksheet.freeze_panes(1, 0)


# ═══════════════════════════════════════════════════════════════════════════
# Mismatch group serialisation
# ═══════════════════════════════════════════════════════════════════════════
# The DataFrame inside each group needs to round-trip via JSON for the
# browser, which only deals with primitive types.

def _trunc2(text: str) -> str:
    """First two whitespace-separated words of ``text``."""
    words = str(text).split()
    return " ".join(words[:2])


def expected_flags(mm: "pd.DataFrame",
                   brand_override_rules: list[dict[str, Any]]) -> "pd.Series":
    """
    Mirror of _expected_flags in pages/2_Phase_3_Pipeline_and_QC.py.

    Marks rows that match one of the well-known expected-difference
    patterns so the UI can grey them out and the analyst can focus on
    the genuine mismatches.
    """
    t_up = mm["TOOL_BRAND"].astype(str).str.upper()
    b_up = mm["BRAND"].astype(str).str.upper()
    # When BRAND is PRIVATE LABEL, only treat the row as expected if
    # TOOL_BRAND is also in the safe set — PRIVATE LABEL / EXCLUDE(D) /
    # RESTRICTED.  Without this gate a PRIVATE-LABEL brand vs a genuine
    # different tool brand (e.g. "AO BRANDS") was being greyed even though
    # it's a real mismatch.
    pl_safe_tool_brand = (
        t_up.str.startswith("PRIVATE LABEL", na=False)
        | t_up.str.contains("EXCLUDE", na=False)
        | t_up.str.contains("RESTRICTED", na=False)
    )
    flag = (
        (t_up == b_up + " RESTRICTED")
        | t_up.str.contains("EXCLUDE", na=False)
        | t_up.str.startswith("PRIVATE LABEL", na=False)
        | (b_up.str.startswith("PRIVATE LABEL", na=False) & pl_safe_tool_brand)
    )
    override_set: set[tuple[str, str]] = set()
    for row in brand_override_rules or []:
        fb = str(row.get("From Brand", "")).strip().upper()
        to = str(row.get("To TOOL_BRAND", "")).strip().upper()
        if fb and to:
            override_set.add((fb, to))
    if override_set:
        pairs = list(zip(b_up, t_up))
        flag = flag | pd.Series(
            [p in override_set for p in pairs], index=mm.index,
        )
    return flag


def build_mismatch_display(grp: dict[str, Any],
                           main_df: Optional["pd.DataFrame"]
                           ) -> "pd.DataFrame":
    """
    Pre-populate BRAND_NEW / TOOL_BRAND_NEW and (when ``main_df`` carries
    the right columns) attach DESCRIPTION + RMRR enrichment columns.
    """
    mm = grp["mismatch_df"].copy()
    mm["BRAND_NEW"]      = mm["BRAND"]
    mm["TOOL_BRAND_NEW"] = mm["TOOL_BRAND"]

    if main_df is None:
        return mm

    col_upper = {str(c).upper(): c for c in main_df.columns}
    b_col  = col_upper.get(grp["brand_col"].upper(),      grp["brand_col"])
    tb_col = col_upper.get(grp["tool_brand_col"].upper(), grp["tool_brand_col"])

    # Without the matching columns there's nothing to enrich against —
    # return the pre-populated frame unchanged.
    if b_col not in main_df.columns or tb_col not in main_df.columns:
        return mm
    desc_col = col_upper.get("DESCRIPTION")
    rmrr_col = (
        col_upper.get("RAW_US_MULTI_RETAILER_RESTRICTED")
        or col_upper.get("RAW_MULTI_RETAILER_RESTRICTED")
    )

    desc_vals: list[str] = []
    rmrr_vals: list[str] = []
    for _, row in mm.iterrows():
        mask = (
            (main_df[b_col].astype(str).str.upper() == str(row["BRAND"]).upper())
            & (main_df[tb_col].astype(str).str.upper() == str(row["TOOL_BRAND"]).upper())
        )

        if desc_col and desc_col in main_df.columns:
            unique = (
                main_df.loc[mask, desc_col]
                .astype(str).str.strip()
                .loc[lambda s: (s != "") & (s.str.lower() != "nan")]
                .unique().tolist()
            )
            truncated = sorted({_trunc2(d) for d in unique})
            cell = ", ".join(truncated[:5])
            if len(truncated) > 5:
                cell += f" (+{len(truncated) - 5})"
            desc_vals.append(cell)
        else:
            desc_vals.append("")

        if rmrr_col and rmrr_col in main_df.columns:
            has_rmrr = (
                main_df.loc[mask, rmrr_col]
                .astype(str).str.strip()
                .loc[lambda s: (s != "") & (s.str.lower() != "nan")]
                .any()
            )
            rmrr_vals.append("RES" if has_rmrr else "")
        else:
            rmrr_vals.append("")

    if any(v for v in desc_vals):
        mm.insert(min(2, len(mm.columns)), "DESCRIPTION", desc_vals)
    if any(v for v in rmrr_vals):
        mm.insert(mm.columns.get_loc("BRAND_NEW"), "RMRR", rmrr_vals)
    return mm


def apply_expected_and_sort(mm: "pd.DataFrame",
                            brand_override_rules: list[dict[str, Any]]
                            ) -> "pd.DataFrame":
    """Compute _is_expected and sort genuine mismatches above expected ones."""
    mm = mm.copy()
    mm["_is_expected"] = expected_flags(mm, brand_override_rules).astype(int)
    sort_cols = ["_is_expected", "BRAND", "TOOL_BRAND"]
    if "PARENT" in mm.columns:
        sort_cols.append("PARENT")
    return mm.sort_values(sort_cols, ascending=True, ignore_index=True)


def serialise_mismatch_groups(
    groups: list[dict[str, Any]],
    main_df: Optional["pd.DataFrame"] = None,
    brand_override_rules: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """
    Convert raw mismatch groups into JSON-safe dicts with the same
    enrichment + sort the Streamlit page applied.

    ``main_df`` and ``brand_override_rules`` are optional so the legacy
    test path (which patched serialise to take just the groups) keeps
    working — callers in production always supply both.
    """
    out: list[dict[str, Any]] = []
    for g in groups:
        enriched = build_mismatch_display(g, main_df)
        sorted_df = apply_expected_and_sort(enriched, brand_override_rules or [])
        out.append({
            "model_suffix":   g.get("model_suffix", ""),
            "brand_col":      g["brand_col"],
            "tool_brand_col": g["tool_brand_col"],
            "parent_col":     g.get("parent_col"),
            "rows":           sorted_df.fillna("").astype(str).to_dict(orient="records"),
        })
    return out


def collect_dropdown_values(
    groups: list[dict[str, Any]],
    main_df: Optional["pd.DataFrame"] = None,
) -> tuple[list[str], list[str]]:
    """
    Distinct BRAND + TOOL_BRAND values to power the wizard's dropdowns.

    Streamlit reads these from the full pipeline df (lines 1305-1314 of
    pages/2_Phase_3_Pipeline_and_QC.py) so the analyst sees every brand
    that exists in the data — not just the mismatched subset.  When the
    df is unavailable we fall back to the values present in the groups.
    """
    if main_df is not None and len(groups) > 0:
        col_upper = {str(c).upper(): c for c in main_df.columns}
        b_col  = col_upper.get(groups[0]["brand_col"].upper(),      groups[0]["brand_col"])
        tb_col = col_upper.get(groups[0]["tool_brand_col"].upper(), groups[0]["tool_brand_col"])
        if b_col in main_df.columns and tb_col in main_df.columns:
            brand_values = sorted(
                main_df[b_col].dropna().astype(str).str.strip()
                .loc[lambda s: (s != "") & (s.str.lower() != "nan")].unique().tolist()
            )
            tb_values = sorted(
                main_df[tb_col].dropna().astype(str).str.strip()
                .loc[lambda s: (s != "") & (s.str.lower() != "nan")].unique().tolist()
            )
            return brand_values, tb_values

    # Fallback: union the values found in the mismatch frames themselves.
    brand_set: set[str] = set()
    tb_set: set[str] = set()
    for g in groups:
        df = g["mismatch_df"]
        for v in df["BRAND"].astype(str):
            if v and v.lower() != "nan":
                brand_set.add(v)
        for v in df["TOOL_BRAND"].astype(str):
            if v and v.lower() != "nan":
                tb_set.add(v)
    return sorted(brand_set), sorted(tb_set)
