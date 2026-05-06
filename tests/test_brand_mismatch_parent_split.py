"""
Phase 2 used to wire one ``raw_manufacturer_col`` knob into two unrelated
jobs: brand-override cleanup *and* the BRAND-vs-TOOL_BRAND mismatch
dialog's PARENT column.  Analysts who set the column to RAW_MANUFACTURER
for the cleanup logic lost the retailer values (CVS, Walmart) the dialog
needed to identify private-label rows.

Step 13 now accepts a dedicated ``raw_parent_col``.  These tests pin the
contract so future changes can't silently regress the split.
"""

from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from phase3_package.quality import check_brand_tool_brand_mismatch


def _df_with_ao_mismatch() -> "pd.DataFrame":
    """
    Three rows:
      - AO-prefixed BRAND that disagrees with TOOL_BRAND (the mismatch we surface)
      - Distinct RAW_PARENT vs RAW_MANUFACTURER values so the test can prove
        which column ends up under the PARENT header.
    """
    return pd.DataFrame([
        {"BRAND": "AO ACME", "TOOL_BRAND": "ACMI",
         "RAW_PARENT": "CVS PHARMACY",        "RAW_MANUFACTURER": "ACME CORP"},
        {"BRAND": "AO ACME", "TOOL_BRAND": "ACMI",
         "RAW_PARENT": "WALMART STORES",      "RAW_MANUFACTURER": "ACME CORP"},
        {"BRAND": "OMEGA",   "TOOL_BRAND": "OMEGA",
         "RAW_PARENT": "WALMART STORES",      "RAW_MANUFACTURER": "OMEGA INC"},
    ])


def test_raw_parent_col_drives_dialog_parent_column() -> None:
    groups = check_brand_tool_brand_mismatch(
        _df_with_ao_mismatch(),
        raw_parent_col="RAW_PARENT",
    )
    assert len(groups) == 1
    g = groups[0]
    assert g["parent_col"] == "RAW_PARENT"
    parents = set(g["mismatch_df"]["PARENT"])
    assert parents == {"CVS PHARMACY", "WALMART STORES"}


def test_raw_parent_col_takes_precedence_over_manufacturer_fallback() -> None:
    """
    Both columns provided → raw_parent_col wins so the dialog stays
    consistent with PL/CVS detection.  Without this, an analyst tweaking
    the manufacturer dropdown could silently steal the dialog's PARENT
    column away from RAW_PARENT.
    """
    groups = check_brand_tool_brand_mismatch(
        _df_with_ao_mismatch(),
        raw_manufacturer_col="RAW_MANUFACTURER",
        raw_parent_col="RAW_PARENT",
    )
    assert groups[0]["parent_col"] == "RAW_PARENT"
    assert "CVS PHARMACY" in set(groups[0]["mismatch_df"]["PARENT"])


def test_legacy_manufacturer_only_callers_still_work() -> None:
    """
    Older callers that only pass raw_manufacturer_col fall back to the
    pre-split behavior (manufacturer column rendered as PARENT) so they
    don't silently break before they're migrated.
    """
    groups = check_brand_tool_brand_mismatch(
        _df_with_ao_mismatch(),
        raw_manufacturer_col="RAW_MANUFACTURER",
    )
    assert groups[0]["parent_col"] == "RAW_MANUFACTURER"
    assert set(groups[0]["mismatch_df"]["PARENT"]) == {"ACME CORP"}


def test_no_parent_col_provided_skips_parent_column() -> None:
    groups = check_brand_tool_brand_mismatch(_df_with_ao_mismatch())
    assert groups[0]["parent_col"] is None
    assert "PARENT" not in groups[0]["mismatch_df"].columns
