"""
Validation tests for ml_package.routing — the openness-based router that decides,
per attribute, whether the learned methods (the LinearSVC classifier in
ml_classifier.py and BM25 in text_match.py) run, or whether the attribute is an
identity/derived composite that Lookup should carry alone.

Lives in the integration suite because it imports the *real* ml_package (the fast
tests/conftest.py stubs ml_package out, so the routing module isn't importable
there).
"""
from __future__ import annotations

import pandas as pd

from ml_package import routing


def _identity_history() -> pd.DataFrame:
    """Each (brand, packtype) combo maps to its OWN label → openness == 1.0
    (a deterministic identity composite, like Franchise_Packtype_RPTG)."""
    rows = [(f"brand{i}", f"pk{i}", f"brand{i}_pk{i}") for i in range(50)]
    return pd.DataFrame(rows, columns=["brand", "packtype", "combo_label"])


def _classification_history() -> pd.DataFrame:
    """2 labels reused across 100 distinct key values → openness == 0.02
    (a genuine classification, like Packtype)."""
    rows = [(f"k{i}", "A" if i % 2 == 0 else "B") for i in range(100)]
    return pd.DataFrame(rows, columns=["key", "label"])


def test_openness_identity_is_one():
    openness, n_labels, n_combos = routing.label_openness(
        _identity_history(), ["brand", "packtype"], "combo_label")
    assert (n_labels, n_combos) == (50, 50)
    assert openness == 1.0


def test_openness_classification_is_low():
    openness, n_labels, n_combos = routing.label_openness(
        _classification_history(), ["key"], "label")
    assert (n_labels, n_combos) == (2, 100)
    assert openness < 0.1


def test_skip_identity_composite():
    skip, reason = routing.skip_learned_methods(
        _identity_history(), ["brand", "packtype"], "combo_label")
    assert skip is True
    assert "identity" in reason.lower()


def test_keep_genuine_classification():
    skip, reason = routing.skip_learned_methods(
        _classification_history(), ["key"], "label")
    assert skip is False
    assert reason == ""


def test_cardinality_backstop_fires_below_openness_threshold():
    # n_labels above the backstop but openness below the skip threshold: the
    # backstop must still skip the learned methods (memory/runtime protection).
    n_combos = 3000
    n_labels = routing.MAX_LABEL_CLASSES + 500
    df = pd.DataFrame({
        "key":   [f"k{i}" for i in range(n_combos)],
        "label": [f"L{i % n_labels}" for i in range(n_combos)],
    })
    openness, nl, nc = routing.label_openness(df, ["key"], "label")
    assert openness < routing.OPENNESS_SKIP_THRESHOLD   # not caught by openness
    assert nl > routing.MAX_LABEL_CLASSES               # caught by the backstop
    skip, reason = routing.skip_learned_methods(df, ["key"], "label")
    assert skip is True
    assert "backstop" in reason.lower()


def test_missing_key_column_is_safe():
    df = _classification_history()
    assert routing.label_openness(df, ["does_not_exist"], "label") == (0.0, 0, 0)
    skip, reason = routing.skip_learned_methods(df, ["does_not_exist"], "label")
    assert skip is False
