"""
Assortment Mapping output writer — single workbook, optimised write path.

Two write modes:
  - Raw frames   → write_row() per row (one Python call per row vs one per cell,
                   ~91x fewer calls for 91-column DataFrames)
  - Analyst lkp  → cell-by-cell (needed for conditional colour formatting)
"""

import math
import pandas as pd
import xlsxwriter


def _clean(v):
    """Replace float NaN/INF and the string 'nan' with empty string.

    attrGridPDF is coerced with fillna('nan') before the pipeline runs, so
    null values arrive here as the literal string 'nan'.  Writing those as
    blank cells means Phase 2's fill condition (== '' | isna()) correctly
    identifies them as unfilled and applies the lookup suggestion.
    """
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return ''
    if isinstance(v, str) and v.lower() == 'nan':
        return ''
    return v


# ── colour palette ────────────────────────────────────────────────────────────
_HDR_BG   = "#1F4E79"
_HDR_FG   = "#FFFFFF"
_SCORELO  = "#FFEB9C"   # amber  — score<100 AND misaligned
_ALT_ROW  = "#F2F2F2"
_CONF_LOW = "#FFC7CE"   # red    — LOW confidence


def _write_raw_sheet(workbook, ws_name: str, df: pd.DataFrame) -> None:
    """Fast write for unformatted data sheets using write_row()."""
    ws = workbook.add_worksheet(ws_name[:31])
    hdr_fmt = workbook.add_format({
        "bold": True, "bg_color": _HDR_BG, "font_color": _HDR_FG,
        "border": 1, "text_wrap": True, "valign": "vcenter",
    })
    ws.write_row(0, 0, list(df.columns), hdr_fmt)
    for r, row_data in enumerate(df.values.tolist(), start=1):
        ws.write_row(r, 0, [_clean(v) for v in row_data])
    ws.freeze_panes(1, 0)
    for c, col in enumerate(df.columns):
        try:
            data_max = int(df[col].astype(str).str.len().max())
        except Exception:
            data_max = 10
        width = min(max(len(str(col)), data_max, 10) + 2, 60)
        ws.set_column(c, c, width)


def _write_lkp_sheet(workbook, attr: str, df: pd.DataFrame) -> None:
    """Write one analyst lkp sheet with per-cell colour formatting.

    Sheet is named Final_{attr}_lkp (matching the Phase 2 expected convention).
    The attribute column keeps its original name (e.g. 'BRAND') so Phase 2 can
    read it directly.  Only the ML column is renamed to 'ML Suggestion' for
    analyst clarity.  A hidden 'Rank' column is written at the far right for
    Phase 2 machine-readable filtering (Rank==1 = best suggestion per key).
    """
    ws = workbook.add_worksheet(f"Final_{attr}_lkp"[:31])

    hdr_fmt = workbook.add_format({
        "bold": True, "bg_color": _HDR_BG, "font_color": _HDR_FG,
        "border": 1, "text_wrap": True, "valign": "vcenter",
    })
    cell_fmt     = workbook.add_format({"border": 0, "valign": "top"})
    alt_fmt      = workbook.add_format({"bg_color": _ALT_ROW, "border": 0, "valign": "top"})
    amber_fmt    = workbook.add_format({"bg_color": _SCORELO, "border": 0})
    num_fmt      = workbook.add_format({"num_format": "0", "border": 0})
    conf_low_fmt = workbook.add_format({
        "bg_color": _CONF_LOW, "bold": True, "border": 0,
        "align": "center", "font_color": "#9C0006",
    })

    display_df = df.copy()
    # ML Method carries internal pipeline labels (e.g. "BM25+XGB") that are
    # not meaningful to analysts — drop it before writing.
    if 'ML Method' in display_df.columns:
        display_df.drop(columns=['ML Method'], inplace=True)
    # Rename only the ML column — the attribute column keeps its original name
    # (e.g. 'BRAND') so Phase 2 can read it by attribute name without mapping.
    if f"ML{attr}" in display_df.columns:
        display_df.rename(columns={f"ML{attr}": "ML Suggestion"}, inplace=True)
    # Sort: HIGH priority first, then ML Matches Lookup No before Yes (ambers first),
    # then score ascending (lowest confidence at top) — analysts work top-to-bottom
    # through the riskiest items.
    _priority_order  = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    _ml_match_order  = {"No": 0, "Yes": 1}
    if "QC Priority" in display_df.columns:
        display_df["_pri_sort"] = display_df["QC Priority"].map(_priority_order).fillna(3)
        if "ML Matches Lookup" in display_df.columns:
            display_df["_ml_sort"] = display_df["ML Matches Lookup"].map(_ml_match_order).fillna(2)
        _sort_cols  = ["_pri_sort"]
        _sort_asc   = [True]
        if "_ml_sort" in display_df.columns:
            _sort_cols.append("_ml_sort")
            _sort_asc.append(True)   # No before Yes
        if "score" in display_df.columns:
            _sort_cols.append("score");  _sort_asc.append(True)    # ascending — least confident first
        _sort_cols = [c for c in _sort_cols if c in display_df.columns]
        _sort_asc  = _sort_asc[:len(_sort_cols)]
        display_df = display_df.sort_values(_sort_cols, ascending=_sort_asc).drop(
            columns=[c for c in ["_pri_sort", "_ml_sort"] if c in display_df.columns]
        ).reset_index(drop=True)
    elif "score" in display_df.columns:
        display_df = display_df.sort_values("score", ascending=False).reset_index(drop=True)

    note_fmt = workbook.add_format({
        "bg_color": "#FFF2CC", "italic": True, "border": 0,
        "font_color": "#7F6000", "text_wrap": True, "valign": "top",
    })

    cols           = list(display_df.columns)
    score_idx      = cols.index("score")      if "score"      in cols else None
    conf_idx       = cols.index("QC Priority") if "QC Priority" in cols else None
    note_idx       = cols.index("Note")       if "Note"       in cols else None
    ml_agrees_idx  = cols.index("ML Matches Lookup")  if "ML Matches Lookup"  in cols else None

    for c, col in enumerate(cols):
        ws.write(0, c, col, hdr_fmt)

    for r, row in enumerate(display_df.itertuples(index=False), start=1):
        row_fmt = alt_fmt if r % 2 == 0 else cell_fmt
        for c, val in enumerate(row):
            val = _clean(val)   # convert float NaN and string 'nan' → '' before any path writes it
            if c == score_idx:
                try:
                    sv = float(val)
                    # Amber when score < 100 AND ML disagrees — if both agree, treat as correct
                    disagrees = (
                        ml_agrees_idx is not None and
                        str(row[ml_agrees_idx]).strip() == "No"
                    )
                    ws.write_number(r, c, sv, amber_fmt if (sv < 100 and disagrees) else num_fmt)
                    continue
                except (TypeError, ValueError):
                    pass
            if c == ml_agrees_idx:
                sval = str(val).strip()
                _amber_ml = False
                if sval == "No" and score_idx is not None:
                    try:
                        _amber_ml = float(row[score_idx]) < 100
                    except (TypeError, ValueError):
                        pass
                ws.write(r, c, sval, amber_fmt if _amber_ml else row_fmt)
                continue
            if c == conf_idx:
                ws.write(r, c, str(val), conf_low_fmt if str(val) == "HIGH" else cell_fmt)
                continue
            if c == note_idx:
                sval = str(val).strip()
                ws.write(r, c, sval, note_fmt if sval and sval != 'nan' else row_fmt)
                continue
            if isinstance(val, (int, float)) and not (isinstance(val, float) and pd.isna(val)):
                ws.write_number(r, c, val, num_fmt)
            elif pd.isna(val) if not isinstance(val, str) else False:
                ws.write_blank(r, c, None, row_fmt)
            else:
                ws.write(r, c, str(val) if not isinstance(val, str) else val, row_fmt)

    # Freeze row 1 (header) + key columns so scrolling right keeps product context.
    # Key columns are everything before the attribute suggestion column.
    _freeze_col = next(
        (i for i, c in enumerate(cols) if c == attr), 0
    )
    ws.freeze_panes(1, _freeze_col)
    ws.autofilter(0, 0, len(display_df), len(cols) - 1)
    for c, col in enumerate(cols):
        if col == "Rank":
            ws.set_column(c, c, 8)
            continue
        if col == "Note":
            # Notes are intentionally descriptive — fixed width + wrap keeps rows compact
            ws.set_column(c, c, 45)
            continue
        try:
            data_max = int(display_df[col].astype(str).str.len().max())
        except Exception:
            data_max = 10
        width = max(len(str(col)), data_max, 10)
        if col in (attr, "ML Suggestion"):
            width = max(width, 22)
        ws.set_column(c, c, min(width + 2, 60))


def write_results(out_path: str,
                  FINAL, FLAT_FILE_OUT,
                  metaGridPDF_old, dictEnsemble,
                  acc_rows=None, fill_summary=None,
                  lkp_rows=None, ml_rows=None) -> int:
    """
    Write pipeline outputs to one Excel workbook (File_For_Mapping_QC.xlsx).
    Sheets written: FINAL (column template for Phase 3), FLAT_FILE, META,
    one analyst lkp sheet per attribute, and optional diagnostics.
    Returns total row count written.
    """
    workbook = xlsxwriter.Workbook(out_path)
    total_rows = 0

    # ── raw frames (fast write_row path) ─────────────────────────────────────
    for name, df in [
        ("FINAL",     FINAL),
        ("FLAT_FILE", FLAT_FILE_OUT),
        ("META",      metaGridPDF_old),
    ]:
        _write_raw_sheet(workbook, name, df)
        total_rows += len(df)

    # ── analyst lkp sheets (formatted) ───────────────────────────────────────
    for key, df in dictEnsemble.items():
        attr = key.replace("Final_", "").replace("_lkp", "")
        _write_lkp_sheet(workbook, attr, df)
        total_rows += len(df)

    # ── diagnostic sheets ─────────────────────────────────────────────────────
    for name, rows in [
        ("qc_burden",      acc_rows),
        ("fill_summary",   fill_summary),
        ("lookup_quality", lkp_rows),
        ("ml_quality",     ml_rows),
    ]:
        if rows:
            _write_raw_sheet(workbook, name, pd.DataFrame(rows))

    workbook.close()
    return total_rows
