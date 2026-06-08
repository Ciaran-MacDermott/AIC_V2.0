#!/usr/bin/env python
# coding: utf-8
"""
Attribute routing for Phase 1 — decides, per MODELING attribute, whether the
learned methods (the ML classifier in ml_classifier.py and BM25 in
text_match.py) should run at all, or whether the attribute is better left to
Lookup.

The signal is *openness* = (distinct labels) / (distinct key-combinations) in
the historical data:

  * openness ≈ 1  → the label is essentially a deterministic function of the key
    columns: an identity / derived composite (e.g. Franchise_Packtype_RPTG, where
    each (franchise × pack_type) pair IS its own label).  No classifier or
    retriever can generalise to a genuinely-new combo there — the *label* would
    be new too — so the learned methods are pure cost (and, on large label
    spaces, a memory/runtime hazard).  Lookup carries it.

  * openness ≪ 1  → a small label vocabulary reused across many inputs: a genuine
    classification (e.g. Packtype, Tool_Franchise_TH).  The learned methods earn
    their keep on unseen products, so they run.

A hard cardinality backstop also skips pathological label spaces regardless of
openness.  Both signals are computed per-run from the actual data (openness
shifts with the dataset), so routing adapts instead of hard-coding per-attribute
decisions.  This replaces the earlier blunt class-count cap.
"""
from __future__ import annotations

import pandas as pd

# At/above this labels-per-keycombo ratio the attribute is treated as an
# identity/derived composite → learned methods skipped, Lookup carries it.
# 0.90 sits comfortably between observed clusters: identity fields land ~1.0,
# genuine classifications (few labels reused across many inputs) land well below.
OPENNESS_SKIP_THRESHOLD = 0.90

# Hard cardinality backstop — skip the learned methods above this many distinct
# labels even when openness is lower (protects runtime/memory on pathological
# label spaces).  LinearSVC scales well, so this is deliberately high; openness
# is the primary signal.
MAX_LABEL_CLASSES = 2000


def label_openness(history_df: pd.DataFrame, attr_key_cols: list, mdm_col: str):
    """
    Return (openness, n_labels, n_keycombos) for an attribute, from history.

    openness = n_labels / n_keycombos.  ~1 means each key combination maps to its
    own label (the key combo *determines* the label — a deterministic composite).
    """
    cols = [c for c in attr_key_cols if c in history_df.columns]
    if not cols or mdm_col not in history_df.columns:
        return 0.0, 0, 0
    sub = history_df.loc[history_df[mdm_col].notna(), cols + [mdm_col]]
    n_labels = int(sub[mdm_col].nunique())
    n_combos = int(sub[cols].drop_duplicates().shape[0])
    openness = (n_labels / n_combos) if n_combos else 1.0
    return openness, n_labels, n_combos


def skip_learned_methods(history_df: pd.DataFrame, attr_key_cols: list,
                         mdm_col: str):
    """
    Decide whether the ML classifier and BM25 should be skipped for this
    attribute (Lookup carries it).  Returns (skip: bool, reason: str).

    Skips when the attribute is an identity/derived composite
    (openness ≥ OPENNESS_SKIP_THRESHOLD) or its label cardinality exceeds
    MAX_LABEL_CLASSES.
    """
    openness, n_labels, n_combos = label_openness(history_df, attr_key_cols, mdm_col)
    if n_combos and openness >= OPENNESS_SKIP_THRESHOLD:
        return True, (
            f"identity/derived composite — {n_labels} labels across {n_combos} "
            f"key-combos (openness {openness:.2f}); learned methods can't "
            f"generalise, Lookup carries it"
        )
    if n_labels > MAX_LABEL_CLASSES:
        return True, (
            f"{n_labels} distinct labels exceeds backstop {MAX_LABEL_CLASSES}; "
            f"routed via Lookup"
        )
    return False, ""
