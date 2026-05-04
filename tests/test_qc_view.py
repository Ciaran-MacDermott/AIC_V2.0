"""
Unit tests for api.qc_view — sheet payload shaping, edit detection,
flag computation, edit merge round-trip.

Runs against a real `pandas` install (it's a transitive dep of pytest's
plugin ecosystem in this project's venv); the heavier ml_package modules
are still stubbed via conftest.
"""

from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from api import qc_view
from api.schemas import QcEditPayload, QcEditedRow


def _make_lkp_df() -> "pd.DataFrame":
    return pd.DataFrame([
        {"BRAND": "ACME", "MLBRAND": "ACME",  "score": 100,
         "QC Priority": "LOW",    "ML Matches Lookup": "Yes", "Note": ""},
        {"BRAND": "ACMI", "MLBRAND": "ACME",  "score": 88,
         "QC Priority": "MEDIUM", "ML Matches Lookup": "No",  "Note": ""},
        {"BRAND": "?",    "MLBRAND": "ZETA",  "score": 50,
         "QC Priority": "HIGH",   "ML Matches Lookup": "No",  "Note": "double check"},
    ])


def test_attribute_from_sheet_key() -> None:
    assert qc_view.attribute_from_sheet_key("Final_BRAND_lkp") == "BRAND"
    assert qc_view.attribute_from_sheet_key("Final_PACK_SIZE_lkp") == "PACK_SIZE"


def test_sort_lookup_df_priority_then_ml_then_score() -> None:
    df = _make_lkp_df()
    out = qc_view.sort_lookup_df(df)
    assert out.iloc[0]["QC Priority"] == "HIGH"
    assert out.iloc[-1]["QC Priority"] == "LOW"


def test_build_sheet_payload_marks_high_priority_and_low_score() -> None:
    df = _make_lkp_df()
    payload = qc_view.build_sheet_payload("Final_BRAND_lkp", df, edits={})

    assert payload.attribute == "BRAND"
    # High-priority row sorts first.
    assert payload.rows[0]["_row_id"] == "r0"
    assert "high_priority" in payload.row_flags["r0"]
    assert "low_score_no_ml" in payload.row_flags["r0"]
    assert "has_note" in payload.row_flags["r0"]

    # Original values preserved per row_id.
    assert payload.original_values["r0"] == "?"

    # ML<attr> renamed to "ML Suggestion".
    assert "ML Suggestion" in payload.rows[0]
    assert "MLBRAND" not in payload.rows[0]

    # Editable column flag.
    editable_fields = [c.field for c in payload.columns if c.editable]
    assert editable_fields == ["BRAND"]


def test_dropdown_options_union_attr_and_ml_suggestions() -> None:
    df = _make_lkp_df()
    payload = qc_view.build_sheet_payload("Final_BRAND_lkp", df, edits={})

    options = set(payload.attribute_options)
    # Empty option always present so users can clear.
    assert "" in options
    # Every non-blank historic + ML value appears.
    for v in ("ACME", "ACMI", "ZETA"):
        assert v in options


def test_dropdown_options_are_deduped_and_sorted() -> None:
    """Same value appearing many times in the column collapses to one option."""
    df = pd.DataFrame([
        {"BRAND": "ACME", "MLBRAND": "ACME", "score": 100,
         "QC Priority": "LOW", "ML Matches Lookup": "Yes", "Note": ""},
        {"BRAND": "ACME", "MLBRAND": "ACME", "score": 100,
         "QC Priority": "LOW", "ML Matches Lookup": "Yes", "Note": ""},
        {"BRAND": "ZETA", "MLBRAND": "ACME", "score": 95,
         "QC Priority": "LOW", "ML Matches Lookup": "No",  "Note": ""},
    ])
    payload = qc_view.build_sheet_payload("Final_BRAND_lkp", df, edits={})

    # 3 input rows → 2 distinct historic values + 1 ML ⇒ {"", ACME, ZETA}.
    assert payload.attribute_options.count("ACME") == 1
    assert payload.attribute_options.count("ZETA") == 1
    # Sorted with empty option first.
    assert payload.attribute_options[0] == ""
    assert payload.attribute_options[1:] == sorted(payload.attribute_options[1:])


def test_dropdown_options_distinct_per_table() -> None:
    """
    Each lookup sheet must get its own dropdown values — BRAND sheet never
    sees PACK_SIZE values and vice versa.  This is the main user-visible
    invariant of the QC wizard: a single sheet's editor lists only what
    that attribute could legitimately be set to.
    """
    brand_df = pd.DataFrame([
        {"BRAND": "ACME", "MLBRAND": "ZETA", "score": 80,
         "QC Priority": "LOW", "ML Matches Lookup": "No", "Note": ""},
    ])
    pack_df = pd.DataFrame([
        {"PACK_SIZE": "12 OZ", "MLPACK_SIZE": "24 OZ", "score": 70,
         "QC Priority": "LOW", "ML Matches Lookup": "No", "Note": ""},
    ])

    brand_payload = qc_view.build_sheet_payload("Final_BRAND_lkp", brand_df, edits={})
    pack_payload  = qc_view.build_sheet_payload("Final_PACK_SIZE_lkp", pack_df, edits={})

    brand_opts = set(brand_payload.attribute_options)
    pack_opts  = set(pack_payload.attribute_options)

    # Each sheet's editor lists only its own attribute's values + ML suggestions.
    assert brand_opts == {"", "ACME", "ZETA"}
    assert pack_opts  == {"", "12 OZ", "24 OZ"}

    # No bleed in either direction.
    assert "ACME"  not in pack_opts
    assert "ZETA"  not in pack_opts
    assert "12 OZ" not in brand_opts
    assert "24 OZ" not in brand_opts


def test_dropdown_excludes_nan_sentinels_and_blanks() -> None:
    """
    The pipeline writes literal 'nan' strings + leaves blanks, both of
    which must NOT appear as dropdown choices (the only blank entry is
    the leading "" so analysts can clear a value).
    """
    df = pd.DataFrame([
        {"BRAND": "ACME", "MLBRAND": "nan", "score": 100,
         "QC Priority": "LOW",  "ML Matches Lookup": "Yes", "Note": ""},
        {"BRAND": "",     "MLBRAND": "ZETA", "score": 50,
         "QC Priority": "HIGH", "ML Matches Lookup": "No",  "Note": ""},
        {"BRAND": "nan",  "MLBRAND": "",    "score": 60,
         "QC Priority": "HIGH", "ML Matches Lookup": "No",  "Note": ""},
    ])
    payload = qc_view.build_sheet_payload("Final_BRAND_lkp", df, edits={})

    assert "nan" not in payload.attribute_options
    # Exactly one blank — the leading clear-value option.
    assert payload.attribute_options.count("") == 1


def test_dropdown_rebuilds_for_each_payload_request() -> None:
    """
    No global state — successive build_sheet_payload calls with
    different inputs must produce independent dropdowns.  Guards
    against accidentally caching options at module import.
    """
    a = qc_view.build_sheet_payload(
        "Final_BRAND_lkp",
        pd.DataFrame([{"BRAND": "ACME", "MLBRAND": "ACME", "score": 100,
                       "QC Priority": "LOW", "ML Matches Lookup": "Yes", "Note": ""}]),
        edits={},
    )
    b = qc_view.build_sheet_payload(
        "Final_BRAND_lkp",
        pd.DataFrame([{"BRAND": "OMEGA", "MLBRAND": "OMEGA", "score": 100,
                       "QC Priority": "LOW", "ML Matches Lookup": "Yes", "Note": ""}]),
        edits={},
    )

    assert "ACME"  in a.attribute_options and "ACME"  not in b.attribute_options
    assert "OMEGA" in b.attribute_options and "OMEGA" not in a.attribute_options


def test_only_attribute_column_is_editable() -> None:
    """The dropdown editor must attach to the attribute column only."""
    df = _make_lkp_df()
    payload = qc_view.build_sheet_payload("Final_BRAND_lkp", df, edits={})

    editable = [c.field for c in payload.columns if c.editable]
    assert editable == ["BRAND"]
    # Every other column must be read-only — these carry pipeline metadata
    # that the analyst should never overwrite.
    non_editable = [c.field for c in payload.columns if not c.editable]
    for protected in ("score", "ML Matches Lookup", "QC Priority", "ML Score",
                      "ML Suggestion", "Note"):
        if protected in [c.field for c in payload.columns]:
            assert protected in non_editable


def test_existing_edits_echo_back_into_payload() -> None:
    df = _make_lkp_df()
    payload = qc_view.build_sheet_payload(
        "Final_BRAND_lkp", df, edits={"r0": "ZETA"},
    )
    high_pri_row = next(r for r in payload.rows if r["_row_id"] == "r0")
    assert high_pri_row["BRAND"] == "ZETA"
    # Original value stays as the *original*, not the edit — that's how
    # the React component detects an edit.
    assert payload.original_values["r0"] == "?"


def test_merge_edits_accumulates_then_apply_yields_edited_df() -> None:
    df = _make_lkp_df()
    record_edits: dict = {}

    qc_view.merge_edits(
        record_edits, "Final_BRAND_lkp",
        QcEditPayload(edited_rows=[QcEditedRow(row_id="r0", attribute_value="ZETA")]),
    )
    qc_view.merge_edits(
        record_edits, "Final_BRAND_lkp",
        QcEditPayload(edited_rows=[
            QcEditedRow(row_id="r0", attribute_value="ZETA_FINAL"),     # later wins
            QcEditedRow(row_id="r1", attribute_value="ACME"),
        ]),
    )

    edits = record_edits["Final_BRAND_lkp"]
    assert edits == {"r0": "ZETA_FINAL", "r1": "ACME"}

    edited_df = qc_view.apply_edits_to_dataframe(
        "Final_BRAND_lkp", df, edits,
    )
    # The edited DataFrame is keyed by display order (post-sort), so r0
    # is the high-priority row and r1 is the medium-priority row.
    assert edited_df.iloc[0]["BRAND"] == "ZETA_FINAL"
    assert edited_df.iloc[1]["BRAND"] == "ACME"
    # MLBRAND restored, not "ML Suggestion".
    assert "MLBRAND" in edited_df.columns
    assert "ML Suggestion" not in edited_df.columns


def test_sheet_summaries_reports_row_and_edit_counts() -> None:
    dict_ensemble = {
        "Final_BRAND_lkp":     _make_lkp_df(),
        "Final_PACK_SIZE_lkp": _make_lkp_df().head(2),
    }
    qc_edits = {
        "Final_BRAND_lkp": {"r0": "X"},
    }

    summaries = qc_view.sheet_summaries(dict_ensemble, qc_edits)
    by_key = {s.key: s for s in summaries}
    assert by_key["Final_BRAND_lkp"].row_count == 3
    assert by_key["Final_BRAND_lkp"].edited_count == 1
    assert by_key["Final_BRAND_lkp"].label == "BRAND"
    assert by_key["Final_PACK_SIZE_lkp"].row_count == 2
    assert by_key["Final_PACK_SIZE_lkp"].edited_count == 0
