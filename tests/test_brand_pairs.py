"""
Brand-pair autodetection from Attributes.txt's Brand_Attribute=Y row.

The pipeline no longer takes brand_col / tool_brand_col / pl_base_name
config fields — the brand pair for each model is the literal column
named on the Attributes.txt Brand_Attribute=Y row (e.g.
TOOL_BRAND_FOOD), with the BRAND-side derived by stripping TOOL_.

These tests pin:
  • PL retagging targets only the resolved tool columns
  • Brand-override rules iterate over the resolved pairs
  • Legacy fallback (no Brand_Attribute column) still discovers TOOL_*/base
    pairs from the DataFrame so older projects keep working
"""

from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from phase3_package.transforms import (
    apply_private_label_rules,
    apply_brand_overrides,
)


_PL_CONFIG = {"cvs": {"enabled": True, "label": "PL_CVS"}}


def _multi_model_df() -> "pd.DataFrame":
    return pd.DataFrame([
        {
            "RAW_PARENT": "CVS PHARMACY",
            "RAW_MANUFACTURER": "ACME",
            # FOOD model: brand pair is BRAND_FOOD / TOOL_BRAND_FOOD
            "BRAND_FOOD":         "ACME-FOOD",
            "TOOL_BRAND_FOOD":    "PRIVATE LABEL",
            # MULO model: brand pair is BRAND_MULO / TOOL_BRAND_MULO
            "BRAND_MULO":         "ACME-MULO",
            "TOOL_BRAND_MULO":    "PRIVATE LABEL",
            # DRUG model: brand pair is SUB_BRAND_DRUG / TOOL_SUB_BRAND_DRUG
            "SUB_BRAND_DRUG":      "ACME-DRUG",
            "TOOL_SUB_BRAND_DRUG": "PRIVATE LABEL",
            # An adjacent attribute that should never get PL labels
            "TOOL_FLAVOR_FOOD":    "VANILLA",
        },
    ])


def test_pl_retag_targets_resolved_tool_columns_only() -> None:
    """
    With brand_pairs covering FOOD + MULO + DRUG, only those three
    TOOL_* columns receive the PL label.  Adjacent TOOL_FLAVOR_FOOD
    must stay untouched.
    """
    df = _multi_model_df()
    pairs = [
        ("BRAND_FOOD",     "TOOL_BRAND_FOOD"),
        ("BRAND_MULO",     "TOOL_BRAND_MULO"),
        ("SUB_BRAND_DRUG", "TOOL_SUB_BRAND_DRUG"),
    ]
    result = apply_private_label_rules(df, _PL_CONFIG, brand_pairs=pairs)
    assert (result["TOOL_BRAND_FOOD"]      == "PL_CVS").all()
    assert (result["TOOL_BRAND_MULO"]      == "PL_CVS").all()
    assert (result["TOOL_SUB_BRAND_DRUG"]  == "PL_CVS").all()
    # Adjacent TOOL_* attribute is not in any brand pair → untouched
    assert (result["TOOL_FLAVOR_FOOD"]     == "VANILLA").all()


def test_pl_retag_falls_back_when_no_brand_pairs() -> None:
    """
    Legacy projects without Brand_Attribute → brand_pairs is None →
    pipeline auto-discovers TOOL_*/base pairs from the DataFrame.
    """
    df = pd.DataFrame([
        {
            "RAW_PARENT": "CVS PHARMACY",
            "TOOL_BRAND": "PRIVATE LABEL",
            "BRAND":      "X",
        },
    ])
    result = apply_private_label_rules(df, _PL_CONFIG)
    assert (result["TOOL_BRAND"] == "PL_CVS").all()


def test_brand_overrides_iterate_resolved_pairs() -> None:
    """
    apply_brand_overrides drives off brand_pairs too — rules rewrite
    only the resolved TOOL_* columns, never the wrong-suffix variants.
    """
    df = pd.DataFrame([
        {
            "RAW_MANUFACTURER":   "ACME",
            "BRAND_FOOD":         "ACME-FOOD",
            "TOOL_BRAND_FOOD":    "WRONG-FOOD",
            "BRAND_MULO":         "ACME-MULO",
            "TOOL_BRAND_MULO":    "WRONG-MULO",
        },
    ])
    config = {
        "enable": True,
        "raw_manufacturer_col": "RAW_MANUFACTURER",
        "rules": [
            {
                "manufacturers":   ["ACME"],
                "brand_overrides": {"ACME-FOOD": "FIXED-FOOD", "ACME-MULO": "FIXED-MULO"},
            },
        ],
    }
    pairs = [
        ("BRAND_FOOD", "TOOL_BRAND_FOOD"),
        ("BRAND_MULO", "TOOL_BRAND_MULO"),
    ]
    result = apply_brand_overrides(df, config, brand_pairs=pairs)
    assert (result["TOOL_BRAND_FOOD"] == "FIXED-FOOD").all()
    assert (result["TOOL_BRAND_MULO"] == "FIXED-MULO").all()


def test_brand_overrides_legacy_fallback() -> None:
    """
    No brand_pairs → fall back to discovering BRAND/TOOL_BRAND-shaped
    pairs in the DataFrame.  Older projects without Brand_Attribute
    keep working.
    """
    df = pd.DataFrame([
        {
            "RAW_MANUFACTURER":  "ACME",
            "BRAND":             "ACME-X",
            "TOOL_BRAND":        "WRONG",
        },
    ])
    config = {
        "enable": True,
        "raw_manufacturer_col": "RAW_MANUFACTURER",
        "rules": [
            {"manufacturers": ["ACME"], "brand_overrides": {"ACME-X": "FIXED"}},
        ],
    }
    result = apply_brand_overrides(df, config)
    assert (result["TOOL_BRAND"] == "FIXED").all()
