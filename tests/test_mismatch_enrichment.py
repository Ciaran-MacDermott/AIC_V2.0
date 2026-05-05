"""
Parity tests for mismatch-group enrichment.

The Streamlit page does three things to each mismatch group before
showing it to the analyst (see _build_mismatch_display + _expected_flags
+ _apply_expected_and_sort in pages/2_Phase_3_Pipeline_and_QC.py):

  1. Pre-populate BRAND_NEW / TOOL_BRAND_NEW with the originals.
  2. Compute a DESCRIPTION cell from the FIRST TWO WORDS of every distinct
     description in the main df where (BRAND, TOOL_BRAND) matches the row.
  3. Add an RMRR cell ('RES' / '') flagging multi-retailer-restricted SKUs.
  4. Mark each row as _is_expected when it matches one of the well-known
     'expected difference' patterns (PRIVATE LABEL prefix, RESTRICTED
     suffix, EXCLUDE in TOOL_BRAND, configured override) and sort the
     genuine mismatches first.

The refactor must produce the same payload so the React grid can render
the same dropdowns + greyed-out rows the Streamlit version did.
"""

from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from api.pipeline_phase2 import (
    apply_expected_and_sort,
    build_mismatch_display,
    collect_dropdown_values,
    expected_flags,
    serialise_mismatch_groups,
)


def _main_df() -> "pd.DataFrame":
    return pd.DataFrame([
        {"BRAND": "ACME",  "TOOL_BRAND": "ACMI",
         "DESCRIPTION": "ACME widget red premium edition",
         "RAW_US_MULTI_RETAILER_RESTRICTED": ""},
        {"BRAND": "ACME",  "TOOL_BRAND": "ACMI",
         "DESCRIPTION": "ACME widget blue",
         "RAW_US_MULTI_RETAILER_RESTRICTED": "Y"},
        {"BRAND": "ACME",  "TOOL_BRAND": "ACMI",
         "DESCRIPTION": "ACME widget red",   # same first 2 words as row 0
         "RAW_US_MULTI_RETAILER_RESTRICTED": ""},
        {"BRAND": "OMEGA", "TOOL_BRAND": "PRIVATE LABEL RESTRICTED",
         "DESCRIPTION": "OMEGA pl item",
         "RAW_US_MULTI_RETAILER_RESTRICTED": ""},
    ])


def _grp(rows: list[dict]) -> dict:
    return {
        "model_suffix":   "",
        "brand_col":      "BRAND",
        "tool_brand_col": "TOOL_BRAND",
        "parent_col":     None,
        "mismatch_df":    pd.DataFrame(rows),
    }


def test_build_mismatch_display_pre_populates_correction_columns() -> None:
    grp = _grp([{"BRAND": "ACME", "TOOL_BRAND": "ACMI"}])
    out = build_mismatch_display(grp, _main_df())

    assert out.iloc[0]["BRAND_NEW"]      == "ACME"
    assert out.iloc[0]["TOOL_BRAND_NEW"] == "ACMI"


def test_build_mismatch_display_attaches_description_first_two_words() -> None:
    grp = _grp([{"BRAND": "ACME", "TOOL_BRAND": "ACMI"}])
    out = build_mismatch_display(grp, _main_df())

    desc = out.iloc[0]["DESCRIPTION"]
    # Two distinct first-2-word descriptions: 'ACME widget' (rows 0+2) and
    # 'ACME widget' again — collapses to one entry.  Row 1 is also 'ACME widget'.
    assert "ACME widget" in desc
    # Should never include words past index 2.
    assert "premium" not in desc
    assert "blue" not in desc and "red" not in desc


def test_build_mismatch_display_caps_description_at_five_with_overflow() -> None:
    """If more than 5 distinct first-2-word descriptions, append '(+N)'."""
    rows_main = []
    for i in range(7):
        rows_main.append({
            "BRAND": "ACME", "TOOL_BRAND": "ACMI",
            "DESCRIPTION": f"acme variant{i} more text",
            "RAW_US_MULTI_RETAILER_RESTRICTED": "",
        })
    main_df = pd.DataFrame(rows_main)

    grp = _grp([{"BRAND": "ACME", "TOOL_BRAND": "ACMI"}])
    out = build_mismatch_display(grp, main_df)

    desc = out.iloc[0]["DESCRIPTION"]
    assert "(+2)" in desc, desc   # 7 distinct, 5 shown, 2 overflowed


def test_build_mismatch_display_attaches_rmrr_flag() -> None:
    grp = _grp([{"BRAND": "ACME", "TOOL_BRAND": "ACMI"}])
    out = build_mismatch_display(grp, _main_df())

    assert "RMRR" in out.columns
    assert out.iloc[0]["RMRR"] == "RES"


def test_build_mismatch_display_omits_rmrr_when_no_flagged_rows() -> None:
    """No RMRR column emitted when nothing in the group has the flag."""
    main_df = _main_df().assign(RAW_US_MULTI_RETAILER_RESTRICTED="")
    grp = _grp([{"BRAND": "ACME", "TOOL_BRAND": "ACMI"}])
    out = build_mismatch_display(grp, main_df)
    assert "RMRR" not in out.columns


def test_expected_flags_marks_restricted_pattern() -> None:
    df = pd.DataFrame([
        {"BRAND": "ACME", "TOOL_BRAND": "ACME RESTRICTED"},        # _is_expected
        {"BRAND": "ACME", "TOOL_BRAND": "ACMI"},                    # genuine
        {"BRAND": "OMEGA", "TOOL_BRAND": "PRIVATE LABEL OMG"},      # _is_expected
        {"BRAND": "PRIVATE LABEL X", "TOOL_BRAND": "PRIVATE LABEL Y"},  # _is_expected
        {"BRAND": "FOO", "TOOL_BRAND": "FOO EXCLUDE NOW"},          # _is_expected
        # Regression: a PRIVATE LABEL brand against a genuinely different
        # tool brand must NOT be flagged as expected.
        {"BRAND": "PRIVATE LABEL", "TOOL_BRAND": "AO BRANDS"},      # genuine
    ])

    flags = expected_flags(df, brand_override_rules=[])
    assert flags.tolist() == [True, False, True, True, True, False]


def test_expected_flags_includes_configured_brand_overrides() -> None:
    """Overrides registered in the Phase 2 config also count as expected."""
    df = pd.DataFrame([
        {"BRAND": "ACME", "TOOL_BRAND": "ACMI"},
        {"BRAND": "ACME", "TOOL_BRAND": "ZETA"},
    ])
    rules = [{"From Brand": "ACME", "To TOOL_BRAND": "ACMI"}]

    flags = expected_flags(df, brand_override_rules=rules)
    assert flags.tolist() == [True, False]


def test_apply_expected_and_sort_puts_genuine_mismatches_first() -> None:
    df = pd.DataFrame([
        {"BRAND": "ACME", "TOOL_BRAND": "ACME RESTRICTED"},   # expected
        {"BRAND": "ACME", "TOOL_BRAND": "ACMI"},              # genuine
        {"BRAND": "BRAVO","TOOL_BRAND": "BRAVO RESTRICTED"},  # expected
        {"BRAND": "ZETA", "TOOL_BRAND": "ZETI"},              # genuine
    ])
    out = apply_expected_and_sort(df, brand_override_rules=[])

    # Genuine rows appear first.
    assert out.iloc[0]["TOOL_BRAND"] == "ACMI"
    assert out.iloc[1]["TOOL_BRAND"] == "ZETI"
    # _is_expected is included so the React grid can grey those rows out.
    assert "_is_expected" in out.columns
    assert int(out.iloc[0]["_is_expected"]) == 0
    assert int(out.iloc[-1]["_is_expected"]) == 1


def test_collect_dropdown_values_pulls_unique_brands_from_main_df() -> None:
    """Dropdown options come from the FULL pipeline df, not just the mismatched subset."""
    main_df = pd.DataFrame([
        {"BRAND": "ACME",  "TOOL_BRAND": "ACME"},
        {"BRAND": "ZETA",  "TOOL_BRAND": "ZETA"},
        {"BRAND": "OMEGA", "TOOL_BRAND": "OMEGA RESTRICTED"},
        {"BRAND": "ACME",  "TOOL_BRAND": "ACME"},   # duplicate — collapses
        {"BRAND": "",      "TOOL_BRAND": "nan"},     # blanks dropped
    ])
    grp = _grp([{"BRAND": "ACME", "TOOL_BRAND": "ACMI"}])  # only one mismatch

    brand_values, tb_values = collect_dropdown_values([grp], main_df=main_df)

    # Every legitimate value from main_df is offered, in sorted order.
    assert brand_values == ["ACME", "OMEGA", "ZETA"]
    assert tb_values    == ["ACME", "OMEGA RESTRICTED", "ZETA"]


def test_collect_dropdown_values_falls_back_to_groups_when_no_main_df() -> None:
    grp = _grp([
        {"BRAND": "ACME", "TOOL_BRAND": "ACMI"},
        {"BRAND": "ZETA", "TOOL_BRAND": "ZETI"},
    ])
    brand_values, tb_values = collect_dropdown_values([grp], main_df=None)
    assert "ACME" in brand_values and "ZETA" in brand_values
    assert "ACMI" in tb_values   and "ZETI" in tb_values


def test_serialise_groups_round_trips_enriched_fields() -> None:
    """End-to-end check: the JSON-safe payload carries the new columns."""
    grp = _grp([
        {"BRAND": "ACME", "TOOL_BRAND": "ACMI"},
        {"BRAND": "ACME", "TOOL_BRAND": "ACME RESTRICTED"},
    ])

    out = serialise_mismatch_groups([grp], main_df=_main_df(), brand_override_rules=[])
    assert len(out) == 1
    rows = out[0]["rows"]
    assert {"BRAND_NEW", "TOOL_BRAND_NEW"} <= set(rows[0].keys())
    # Genuine mismatch sorts to the top.
    assert rows[0]["TOOL_BRAND"] == "ACMI"
    assert rows[1]["TOOL_BRAND"] == "ACME RESTRICTED"
    # _is_expected propagates so the UI can grey expected rows.
    assert int(rows[0].get("_is_expected", "0")) == 0
    assert int(rows[1].get("_is_expected", "0")) == 1
