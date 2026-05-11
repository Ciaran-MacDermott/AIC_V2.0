"""
Server-side QC sheet shaping.

Takes a per-attribute lookup DataFrame from the pipeline and produces
the JSON payload the React grid renders. This mirrors the cleanup steps
the Streamlit QC wizard does inline (sort, drop ML Method, rename
ML<attr> → ML Suggestion, replace 'nan' with '') and pre-computes the
flag tokens the cellStyle in the React component reacts to.

Edit detection happens client-side: the payload includes the original
attribute value per row_id, so the grid can compare against the live
edit without a server round-trip per keystroke.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from api.schemas import (
    ColumnDef,
    QcEditPayload,
    QcSheetPayload,
    QcSheetSummary,
)


_PRI_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_ML_ORDER  = {"No": 0, "Yes": 1}


def sort_lookup_df(df: pd.DataFrame) -> pd.DataFrame:
    """HIGH → MEDIUM → LOW priority, No → Yes ML agreement, score asc."""
    df = df.copy()
    sort_cols, sort_asc = [], []
    if "QC Priority" in df.columns:
        df["_s_pri"] = df["QC Priority"].map(_PRI_ORDER).fillna(3)
        sort_cols.append("_s_pri"); sort_asc.append(True)
    if "ML Matches Lookup" in df.columns:
        df["_s_ml"] = df["ML Matches Lookup"].map(_ML_ORDER).fillna(2)
        sort_cols.append("_s_ml"); sort_asc.append(True)
    if "score" in df.columns:
        sort_cols.append("score"); sort_asc.append(True)
    if sort_cols:
        df = df.sort_values(sort_cols, ascending=sort_asc)
        df = df.drop(columns=[c for c in ("_s_pri", "_s_ml") if c in df.columns])
    return df.reset_index(drop=True)


def attribute_from_sheet_key(sheet_key: str) -> str:
    """'Final_BRAND_lkp' → 'BRAND'."""
    return sheet_key.replace("Final_", "").replace("_lkp", "")


def _prepare_display_df(df: pd.DataFrame, attr: str) -> pd.DataFrame:
    df = sort_lookup_df(df)
    if "ML Method" in df.columns:
        df = df.drop(columns=["ML Method"])
    ml_col_raw = f"ML{attr}"
    if ml_col_raw in df.columns:
        df = df.rename(columns={ml_col_raw: "ML Suggestion"})
    df = df.fillna("")
    obj_cols = df.select_dtypes(include="object").columns
    df[obj_cols] = df[obj_cols].replace("nan", "")
    return df.reset_index(drop=True)


def _flags_for_row(row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if str(row.get("QC Priority", "")) == "HIGH":
        flags.append("high_priority")
    if str(row.get("ML Matches Lookup", "")) == "No":
        try:
            score = float(row.get("score", "")) if row.get("score", "") != "" else None
        except (TypeError, ValueError):
            score = None
        if score is not None and score < 100:
            flags.append("low_score_no_ml")
    if row.get("Note") not in (None, "", "nan"):
        flags.append("has_note")
    return flags


def build_sheet_payload(sheet_key: str, df: pd.DataFrame,
                        edits: dict[str, str]) -> QcSheetPayload:
    """
    Convert a pipeline lookup DataFrame into the JSON shape the QC grid
    renders. `edits` is the per-row override map for this sheet so we
    can echo current edits back when the user revisits the sheet.
    """
    attr = attribute_from_sheet_key(sheet_key)
    display_df = _prepare_display_df(df, attr)

    rows: list[dict[str, Any]] = []
    original_values: dict[str, str] = {}
    row_flags: dict[str, list[str]] = {}

    for idx, row in display_df.iterrows():
        row_id = f"r{idx}"
        record = {col: ("" if pd.isna(val) else val) for col, val in row.items()}
        record["_row_id"] = row_id

        original_attr = str(record.get(attr, ""))
        original_values[row_id] = original_attr

        # Apply any in-progress edit so the grid reopens at the user's last value.
        if row_id in edits and attr in record:
            record[attr] = edits[row_id]

        flags = _flags_for_row(record)
        if flags:
            row_flags[row_id] = flags

        rows.append(record)

    columns: list[ColumnDef] = []
    for col in display_df.columns:
        editable = (col == attr)
        col_type = "number" if col in ("score", "ML Score", "Rank") else "text"
        columns.append(ColumnDef(field=col, header=col, editable=editable, type=col_type))

    attr_vals = (
        sorted({str(v) for v in display_df[attr].tolist() if v not in ("", "nan", None)})
        if attr in display_df.columns else []
    )
    ml_vals = (
        sorted({str(v) for v in display_df["ML Suggestion"].tolist() if v not in ("", "nan", None)})
        if "ML Suggestion" in display_df.columns else []
    )
    options = [""] + sorted(set(attr_vals + ml_vals))

    return QcSheetPayload(
        key=sheet_key,
        attribute=attr,
        columns=columns,
        rows=rows,
        attribute_options=options,
        original_values=original_values,
        row_flags=row_flags,
    )


def sheet_summaries(dict_ensemble: dict[str, pd.DataFrame],
                    qc_edits: dict[str, dict[str, str]]) -> list[QcSheetSummary]:
    out: list[QcSheetSummary] = []
    for key, df in dict_ensemble.items():
        edits = qc_edits.get(key, {})
        out.append(QcSheetSummary(
            key=key,
            label=attribute_from_sheet_key(key),
            row_count=int(len(df)),
            edited_count=len(edits),
        ))
    return out


def apply_edits_to_dataframe(sheet_key: str, original_df: pd.DataFrame,
                             edits: dict[str, str]) -> pd.DataFrame:
    """
    Re-shape an edited sheet back into the column layout `write_results`
    expects (so ML Suggestion → ML<attr>, original sort/order preserved).
    """
    attr = attribute_from_sheet_key(sheet_key)
    display = _prepare_display_df(original_df, attr).copy()
    for idx in range(len(display)):
        row_id = f"r{idx}"
        if row_id in edits and attr in display.columns:
            display.at[idx, attr] = edits[row_id]

    # Restore numeric dtypes that AgGrid may have stringified on the way in.
    for nc in ("score", "Rank", "ML Score"):
        if nc in display.columns:
            display[nc] = pd.to_numeric(display[nc], errors="coerce")

    # Rename ML Suggestion back to its raw column name for write_results.
    ml_col_raw = f"ML{attr}"
    if "ML Suggestion" in display.columns and ml_col_raw in original_df.columns:
        display = display.rename(columns={"ML Suggestion": ml_col_raw})

    return display


def merge_edits(record_edits: dict[str, dict[str, str]],
                sheet_key: str, payload: QcEditPayload) -> int:
    """
    Merge the diff submitted by the client into the per-sheet edit map.
    Returns the new total edited count for the sheet.
    """
    sheet_edits = record_edits.setdefault(sheet_key, {})
    for row in payload.edited_rows:
        sheet_edits[row.row_id] = row.attribute_value
    return len(sheet_edits)
