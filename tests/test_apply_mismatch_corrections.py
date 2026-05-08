"""
Focused unit tests for `_apply_mismatch_corrections` in
phase3_package/pipeline.py.

Why this exists: the wizard emits one MismatchCorrection per edited field,
so an analyst editing both BRAND and TOOL_BRAND on the same row produces
two correction objects keyed on the same (brand_old, tool_brand_old) pair.
Earlier the application loop recomputed the row mask after each iteration,
causing the second iteration to match zero rows once the first had mutated
BRAND. The TOOL_BRAND change was silently dropped.

These tests pin the contract:
  * Same-row BRAND + TOOL_BRAND edits both apply.
  * Independent rows continue to work.
  * Drifted column names skip cleanly with a counted warning instead of
    killing the whole batch.
  * Parent narrowing still routes corrections to the right rows.
"""

from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from phase3_package.pipeline import _apply_mismatch_corrections


def _make_df() -> "pd.DataFrame":
    return pd.DataFrame([
        {"BRAND_TGT": "AO BRANDS", "TOOL_BRAND_TGT": "AO CLOROX RESTRICTED",
         "RAW_PARENT": "CLOROX COMPANY"},
        {"BRAND_TGT": "AO BRANDS", "TOOL_BRAND_TGT": "AO CLOROX RESTRICTED",
         "RAW_PARENT": "CLOROX COMPANY"},
        {"BRAND_TGT": "AO BRANDS", "TOOL_BRAND_TGT": "AO PEPSICO",
         "RAW_PARENT": "PEPSICO INC"},
        {"BRAND_TGT": "OMEGA",     "TOOL_BRAND_TGT": "OMEGA",
         "RAW_PARENT": ""},
    ])


def test_same_row_brand_and_tool_brand_edits_both_apply() -> None:
    """Regression: with the old loop, the TOOL_BRAND edit was silently
    dropped because BRAND had already been mutated and the mask no longer
    matched."""
    df = _make_df()

    corrections = [
        {"type": "brand",      "brand": "AO BRANDS",
         "tool_brand_old": "AO CLOROX RESTRICTED",
         "brand_new":      "CLOROX",
         "brand_col": "BRAND_TGT", "tool_brand_col": "TOOL_BRAND_TGT",
         "parent":    "CLOROX COMPANY"},
        {"type": "tool_brand", "brand": "AO BRANDS",
         "tool_brand_old": "AO CLOROX RESTRICTED",
         "tool_brand_new": "AO CLOROX",
         "brand_col": "BRAND_TGT", "tool_brand_col": "TOOL_BRAND_TGT",
         "parent":    "CLOROX COMPANY"},
    ]

    summary = _apply_mismatch_corrections(df, corrections, parent_col_actual="RAW_PARENT")

    # Both rows that matched the (AO BRANDS, AO CLOROX RESTRICTED, CLOROX COMPANY)
    # key got both BRAND_TGT and TOOL_BRAND_TGT updated.
    affected = df[df["RAW_PARENT"] == "CLOROX COMPANY"]
    assert (affected["BRAND_TGT"]      == "CLOROX").all(),    affected["BRAND_TGT"].tolist()
    assert (affected["TOOL_BRAND_TGT"] == "AO CLOROX").all(), affected["TOOL_BRAND_TGT"].tolist()

    # Untouched rows stay untouched.
    assert df.iloc[2]["TOOL_BRAND_TGT"] == "AO PEPSICO"
    assert df.iloc[3]["BRAND_TGT"]      == "OMEGA"

    assert summary["brand_count"] == 1
    assert summary["tool_count"]  == 1
    assert summary["n_pairs"]     == 1
    assert summary["rows_updated"] == 2
    assert summary["skipped"] == 0


def test_independent_row_corrections_apply_separately() -> None:
    df = _make_df()

    corrections = [
        {"type": "tool_brand", "brand": "AO BRANDS",
         "tool_brand_old": "AO PEPSICO", "tool_brand_new": "PEPSICO",
         "brand_col": "BRAND_TGT", "tool_brand_col": "TOOL_BRAND_TGT",
         "parent":    "PEPSICO INC"},
        {"type": "brand", "brand": "OMEGA", "tool_brand_old": "OMEGA",
         "brand_new": "OMEGA NEW",
         "brand_col": "BRAND_TGT", "tool_brand_col": "TOOL_BRAND_TGT",
         "parent": ""},
    ]

    summary = _apply_mismatch_corrections(df, corrections, parent_col_actual="RAW_PARENT")

    assert df.iloc[2]["TOOL_BRAND_TGT"] == "PEPSICO"
    assert df.iloc[3]["BRAND_TGT"]      == "OMEGA NEW"
    # Clorox rows untouched.
    assert df.iloc[0]["BRAND_TGT"] == "AO BRANDS"
    assert summary["n_pairs"] == 2


def test_drifted_column_skips_with_warning_does_not_kill_batch(capsys) -> None:
    df = _make_df()

    corrections = [
        # Bad: column "BRAND_FOO" doesn't exist in df.
        {"type": "brand", "brand": "AO BRANDS",
         "tool_brand_old": "AO CLOROX RESTRICTED",
         "brand_new": "X",
         "brand_col": "BRAND_FOO", "tool_brand_col": "TOOL_BRAND_FOO",
         "parent": "CLOROX COMPANY"},
        # Good: should still apply despite the bad sibling above.
        {"type": "tool_brand", "brand": "AO BRANDS",
         "tool_brand_old": "AO PEPSICO", "tool_brand_new": "PEPSICO",
         "brand_col": "BRAND_TGT", "tool_brand_col": "TOOL_BRAND_TGT",
         "parent": "PEPSICO INC"},
    ]

    summary = _apply_mismatch_corrections(df, corrections, parent_col_actual="RAW_PARENT")

    # Good correction applied.
    assert df.iloc[2]["TOOL_BRAND_TGT"] == "PEPSICO"
    # Bad correction skipped, untouched.
    assert df.iloc[0]["BRAND_TGT"] == "AO BRANDS"

    assert summary["skipped"]   == 1
    assert summary["tool_count"] == 1
    assert summary["brand_count"] == 0

    # Warning printed to stdout for the analyst log.
    captured = capsys.readouterr().out
    assert "Skipping" in captured and "BRAND_FOO" in captured


def test_case_insensitive_column_resolution() -> None:
    """Column matching should be case-insensitive — the wire payload
    can use uppercase even when the df has mixed case."""
    df = pd.DataFrame([
        {"Brand_Tgt": "AO BRANDS", "Tool_Brand_Tgt": "AO CLOROX RESTRICTED"},
    ])

    corrections = [
        {"type": "tool_brand", "brand": "AO BRANDS",
         "tool_brand_old": "AO CLOROX RESTRICTED",
         "tool_brand_new": "AO CLOROX",
         "brand_col": "BRAND_TGT", "tool_brand_col": "TOOL_BRAND_TGT",
         "parent": ""},
    ]

    summary = _apply_mismatch_corrections(df, corrections, parent_col_actual=None)

    assert df.iloc[0]["Tool_Brand_Tgt"] == "AO CLOROX"
    assert summary["skipped"] == 0


def test_empty_corrections_returns_zero_summary() -> None:
    df = _make_df()
    summary = _apply_mismatch_corrections(df, [], parent_col_actual="RAW_PARENT")
    assert summary == {
        "brand_count": 0, "tool_count": 0,
        "rows_updated": 0, "n_pairs": 0, "skipped": 0,
    }
    # df untouched.
    assert df.iloc[0]["BRAND_TGT"] == "AO BRANDS"


def test_parent_narrowing_routes_to_correct_subset() -> None:
    """When two rows have the same (BRAND, TOOL_BRAND) pair but different
    parents, parent narrowing should route corrections to the right one."""
    df = pd.DataFrame([
        {"BRAND_TGT": "AO BRANDS", "TOOL_BRAND_TGT": "AO X",
         "RAW_PARENT": "PARENT A"},
        {"BRAND_TGT": "AO BRANDS", "TOOL_BRAND_TGT": "AO X",
         "RAW_PARENT": "PARENT B"},
    ])

    corrections = [
        {"type": "tool_brand", "brand": "AO BRANDS",
         "tool_brand_old": "AO X", "tool_brand_new": "FIXED",
         "brand_col": "BRAND_TGT", "tool_brand_col": "TOOL_BRAND_TGT",
         "parent": "PARENT A"},
    ]

    _apply_mismatch_corrections(df, corrections, parent_col_actual="RAW_PARENT")

    assert df.iloc[0]["TOOL_BRAND_TGT"] == "FIXED"
    assert df.iloc[1]["TOOL_BRAND_TGT"] == "AO X"  # untouched
