#!/usr/bin/env python
# coding: utf-8
"""
Ensemble — predictor combiner and QC annotator  (Phase 1, Step 3).

This is the decision layer of the pipeline.  By the time data arrives here,
MappingLookup has produced a fuzzy-matched historical suggestion for every
product, and TextMatch + RandomForest_XGB have each produced an independent
ML prediction.  The Ensemble's job is to bring those signals together into a
single ranked output that an analyst can actually act on.

What it does for each attribute
--------------------------------
  1. Determines whether an attribute is NUMERIC or TEXT.  NUMERIC attributes
     (analyst-flagged, or auto-detected from range/quantity key columns like
     "16 OZ") are handled by Lookup only — range values are not text-classifiable.
     Everything else runs BM25 + XGBoost and gets a genuine ML Score.

  2. For TEXT attributes, BM25 and XGB each produce a top-K ranked candidate
     list.  Scores are normalised to the same 0–1 scale (BM25 min-max per
     attribute; XGB predict_proba) and fused: when both methods endorse the same
     label its combined score is amplified (Consensus); when they differ the
     higher-confidence method wins.

  3. ML Score (0–100) is the fused confidence relative to the best prediction
     for that attribute.  A high score means the model has seen enough similar
     products to generalise with confidence.  A low score means the product's
     key-column values are genuinely new or uncoded in the training database —
     the model is being honest about the limits of its training data, not failing.

  4. QC Priority and per-row notes give the analyst the context to act:
     HIGH = needs a look, LOW = safe to trust, notes explain the reason.

Output
------
One DataFrame per attribute stored in dictEnsemble as 'Final_{attrG}_lkp',
ready for _write_results to write to the Excel workbook.

Attribute type system
---------------------
NUMERIC   Auto-detected or analyst-flagged (any non-blank META Type value).
          Lookup suggestion only — no ML applied.  Range/quantity keys like
          "16 OZ" or "22-24 CT" carry no text signal a classifier can use.

VOCAB     Only when the analyst explicitly marks Type as DERIVED or CATEGORICAL
          in META (legacy flag).  Uses rapidfuzz label matching.  No auto-VOCAB
          detection — BM25 + XGB handle small-label-set attributes correctly
          and produce a richer, more interpretable confidence signal.

TEXT      All other attributes.  Full BM25 + XGBoost ensemble with ML Score.

Code style
----------
Functions are written to be read straight through.  Steps are broken into named
variables rather than chained.  If something is not immediately obvious from the
code it has a comment.  Keep it that way.
"""

import re

import numpy as np
import pandas as pd
from rapidfuzz import process as _rfprocess, fuzz as _rffuzz


# ── Numeric range pattern ─────────────────────────────────────────────────────
# Matches values like "10-12 OZ BAG", "22-24 CT PODS", "1.5 LBS", "3# CANNED".
# Rule: if the majority of a key column's unique values start with a digit
# (possibly followed by a range separator), the column carries numeric/range
# data that ML text models cannot reliably bin.
_NUMERIC_RE        = re.compile(r'^\s*[\d#][\d\s\.\-\/]*', re.IGNORECASE)
_NUMERIC_THRESHOLD = 0.60   # >60% of unique non-null values must match

_NULL_VALS = {'', 'nan', 'none', 'missing', 'null', 'na',
              'NaN', 'NAN', 'NONE', 'MISSING', 'NULL', 'NA'}

def _infer_attr_type(attr_key_cols: list, data_df: pd.DataFrame) -> str:
    """
    Auto-detect NUMERIC attributes from key column values.

    Returns 'NUMERIC' if >60% of unique non-null values match the numeric-
    range pattern, otherwise '' so the caller can run further checks.
    """
    for col in attr_key_cols:
        if col not in data_df.columns:
            continue
        stripped_values = data_df[col].dropna().astype(str).str.strip()
        null_placeholder_mask = stripped_values.isin({'', 'nan', 'None', 'missing'})
        non_null_values = stripped_values[~null_placeholder_mask].unique()
        if len(non_null_values) == 0:
            continue
        numeric_match_count = sum(bool(_NUMERIC_RE.match(v)) for v in non_null_values)
        numeric_fraction = numeric_match_count / len(non_null_values)
        if numeric_fraction >= _NUMERIC_THRESHOLD:
            return 'NUMERIC'
    return ''



def _predict_vocab(attr_col: str, attr_key_cols: list,
                   lookup_df: pd.DataFrame, test_df: pd.DataFrame) -> pd.DataFrame:
    """
    Vocab-matching predictor for VOCAB / DERIVED / CATEGORICAL attributes.

    Uses rapidfuzz token_set_ratio to match each test row's concatenated key
    string against the known label vocabulary derived from the Lookup output.
    More accurate than BM25 for short, structured enumeration labels
    (e.g. METHOD_FORM, PACK_TYPE) where IDF weighting hurts more than it helps.

    Returns a DataFrame with columns: attr_key_cols + [attr_col].
    Returns an empty DataFrame when no predictions can be made.
    """
    cols_avail = [c for c in attr_key_cols if c in lookup_df.columns and c in test_df.columns]
    if not cols_avail or attr_col not in lookup_df.columns:
        return pd.DataFrame()

    def _concat_key(df, cols):
        return (
            df[cols].astype(str)
            .apply(
                lambda row: ' '.join(
                    v for v in row if v.strip().lower() not in _NULL_VALS
                ),
                axis=1,
            )
            .str.upper()
            .str.strip()
        )

    # Normalise lookup labels
    lookup_working = lookup_df[cols_avail + [attr_col]].copy()
    if 'score' in lookup_df.columns:
        lookup_working['score'] = lookup_df['score'].values
    for col in cols_avail:
        lookup_working[col] = lookup_working[col].astype(str).str.strip().str.upper()
    lookup_working[attr_col] = lookup_working[attr_col].astype(str).str.strip().str.upper()
    is_null_label = lookup_working[attr_col].str.lower().isin(_NULL_VALS)
    lookup_clean = lookup_working[~is_null_label]

    known_labels = list(lookup_clean[attr_col].unique())
    if not known_labels:
        return pd.DataFrame()

    # Normalise test keys
    test_working = test_df[cols_avail].copy()
    for col in cols_avail:
        test_working[col] = test_working[col].astype(str).str.strip().str.upper()
    test_keys = _concat_key(test_working, cols_avail)

    predictions = []
    for key in test_keys:
        if key and key.lower() not in _NULL_VALS:
            best_match = _rfprocess.extractOne(key, known_labels, scorer=_rffuzz.token_set_ratio)
            predictions.append(best_match[0])
        else:
            predictions.append('')

    output_df = test_working.copy()
    output_df[attr_col] = predictions
    is_non_empty_prediction = output_df[attr_col].str.strip() != ''
    output_df = output_df[is_non_empty_prediction].drop_duplicates(subset=cols_avail)
    return output_df[cols_avail + [attr_col]]


# ── Predictor configuration ───────────────────────────────────────────────────
# Maps attribute type → ML confidence weight (reserved for future weighting)
# and a human-readable description for the Ensemble log line.
_ML_TYPE_CONFIG = {
    'NUMERIC': 'numeric/range',
    'VOCAB':   'fixed-list',
    # Legacy META values (no longer set by analysts, still respected)
    'DERIVED':     'fixed-list',
    'CATEGORICAL': 'fixed-list',
}
_ML_TYPE_DEFAULT = 'text classifier'


def runEnsemble(recom_dict: dict, meta_df: pd.DataFrame,
                flag_map: dict) -> dict:
    """
    Combine Lookup and ML predictor results into one ranked lkp DataFrame
    per attribute, annotated with QC Priority, ML Matches Lookup, and notes.

    Parameters
    ----------
    recom_dict : dict
        All predictor outputs keyed as '{METHOD}_{attrG}'.
    meta_df : DataFrame
        META sheet (MODELING rows only) — attribute group definitions and
        type flags.
    flag_map : dict
        {attrG: DataFrame} mapping from MappingLookup — flags whether a key
        combo maps to one or multiple historical labels.

    Returns
    -------
    dictEnsemble : dict
        {f'Final_{attrG}_lkp': DataFrame} ready for _write_results.
    """
    dict_ensemble = {}

    for attr_col in meta_df['Attribute Group name'].unique():
        attr_key_cols = list(meta_df.loc[meta_df['Attribute Group name'] == attr_col,
                                          'Attribute Name in MDM'])

        split_parent_df = flag_map.get(attr_col)
        if split_parent_df is None:
            print(f"  Ensemble {attr_col}: skipped — no Lookup output found")
            continue

        lookup_df = recom_dict.get(f'Lookup_{attr_col}')
        if lookup_df is None:
            print(f"  Ensemble {attr_col}: skipped — Lookup key missing from recom_dict")
            continue
        lookup_df          = lookup_df.copy()
        lookup_df['method'] = 'Lookup'

        # ── Resolve attribute type ─────────────────────────────────────────
        # Priority: explicit META flag > auto NUMERIC > TEXT.
        # Analysts signal NUMERIC by putting any non-null value in the Type
        # column (e.g. "Y", "NUMERIC") — the exact value doesn't matter.
        # Legacy DERIVED / CATEGORICAL are mapped to VOCAB for backward compat.
        # Auto-VOCAB detection is intentionally disabled: _infer_vocab_type was
        # routing small-label-set attributes to a single rapidfuzz match before
        # BM25+XGB could run, producing ML Score = 100 for every row with no
        # real confidence signal.  BM25+XGB handle these attributes correctly
        # and produce a richer, more accurate ensemble output.
        null_strings = {'', 'NAN', 'NONE', 'NA', 'NULL', 'MISSING'}
        meta_type_vals = (
            meta_df.loc[meta_df['Attribute Group name'] == attr_col, 'Type']
            .dropna()
            .astype(str).str.strip().str.upper()
        )
        meta_type_vals = meta_type_vals[~meta_type_vals.isin(null_strings)]
        meta_type      = meta_type_vals.iloc[0] if len(meta_type_vals) else ''

        if meta_type:
            attr_type = 'VOCAB' if meta_type in ('DERIVED', 'CATEGORICAL') else 'NUMERIC'
            auto_detected = False
        else:
            attr_type     = _infer_attr_type(attr_key_cols, split_parent_df)
            auto_detected = bool(attr_type)

        skip_ml = bool(attr_type)

        # ── Select predictor strategy ─────────────────────────────────────
        ml_results             = pd.DataFrame()
        vocab_predictor_active = False
        methods_used           = []

        if attr_type == 'VOCAB':
            test_keys  = lookup_df[attr_key_cols].drop_duplicates().reset_index(drop=True)
            vocab_preds = _predict_vocab(attr_col, attr_key_cols, lookup_df, test_keys)
            if not vocab_preds.empty:
                vocab_preds         = vocab_preds.copy()
                vocab_preds['method'] = 'VOCAB'
                ml_results          = vocab_preds
                methods_used.append('VOCAB')
                vocab_predictor_active = True
                print(f'  VOCAB    {attr_col}: small fixed label set detected — using direct label matching')

        elif not skip_ml:
            # TEXT: collect ML results for this attribute using exact key names.
            # Only 'BM25_{attr_col}', 'ML_{attr_col}', and 'VOCAB_{attr_col}'
            # are valid ML keys — the old endswith() approach incorrectly matched
            # superset names (e.g. 'BM25_TOOL_BRAND' when processing 'BRAND').
            for method_name in ('BM25', 'ML', 'VOCAB'):
                key = f'{method_name}_{attr_col}'
                if key not in recom_dict:
                    continue
                df      = recom_dict[key]
                df_copy = df.copy()
                df_copy['method'] = method_name
                if ml_results.empty:
                    ml_results = df_copy
                else:
                    ml_results = pd.concat([ml_results, df_copy], ignore_index=True)
                methods_used.append(method_name)

        lookup_df = lookup_df[
            list(set(lookup_df.columns) - {'Record', 'count'})
        ].drop_duplicates()

        # ── Build consensus prediction ────────────────────────────────────
        if ml_results.empty or 'method' not in ml_results.columns:
            # No predictor output — leave ML column blank.
            # NUMERIC: intentional suppression.
            # Others: no method produced output for this attribute.
            ml_results = pd.DataFrame(
                columns=attr_key_cols + [f'ML{attr_col}', 'EnRecord', 'EnRank', 'MLTier', 'ML Method']
            )
        else:
            # ── Normalised score for tiebreaking clashes ──────────────────
            # Extract before stripping columns. Both are mapped to [0, 1]:
            #   BM25 → prob_score (already min-max scaled per attribute)
            #   ML   → score / 100  (classifier confidence × 100; softmax for LinearSVC)
            # Any future method without a recognised score column gets 0.0
            # and will lose any tiebreak against a method that does.
            # Build per-row normalised score using np.where — avoids .loc
            # chained assignment which silently fails under pandas CoW.
            _bm25_scores = pd.to_numeric(
                ml_results['prob_score'] if 'prob_score' in ml_results.columns
                else pd.Series(0.0, index=ml_results.index),
                errors='coerce',
            ).fillna(0.0)
            _xgb_scores = pd.to_numeric(
                ml_results['score'] if 'score' in ml_results.columns
                else pd.Series(0.0, index=ml_results.index),
                errors='coerce',
            ).fillna(0.0) / 100.0
            # VOCAB rows have method == 'VOCAB' and carry no numeric confidence
            # score.  Assigning 1.0 (maximum normalised score) is correct because
            # _predict_vocab already returns the single best-matched label for each
            # key combo — there is no competing candidate to tiebreak against.
            # Without this, VOCAB rows fall through to 0.0 → BestScore = 0 →
            # _max_bs = 0 → ML Score = 0 for every row.
            ml_results = ml_results.assign(
                _norm_score=np.where(
                    ml_results['method'] == 'BM25', _bm25_scores,
                    np.where(ml_results['method'] == 'ML', _xgb_scores,
                    np.where(ml_results['method'] == 'VOCAB', 1.0, 0.0)),
                )
            )

            # Keep only consensus columns + _norm_score — extra columns
            # (prob_score, raw score, ITEM_DIM_KEY, etc.) would create unique
            # groupby keys and prevent BM25/XGB rows being matched as consensus.
            consensus_cols = []
            for c in attr_key_cols + [attr_col, 'method', '_norm_score']:
                if c in ml_results.columns:
                    consensus_cols.append(c)
            ml_results = ml_results[consensus_cols].copy()

            # Normalise case: BM25 lowercases, XGB preserves original — without
            # this "dark roast" != "DARK ROAST" and consensus is never detected.
            for col in attr_key_cols + [attr_col]:
                if col in ml_results.columns:
                    ml_results[col] = ml_results[col].astype(str).str.strip().str.upper()

            def _join_sorted_methods(method_series):
                return '+'.join(sorted(set(method_series)))

            # ── Two-step score-level fusion ────────────────────────────────
            # Step 1: for each (key, label, method) take that method's MAX
            # normalised score.  This collapses multiple products that share
            # the same key columns into one representative score per method,
            # preventing popular key combinations from inflating the sum.
            per_method = (
                ml_results.groupby(attr_key_cols + [attr_col, 'method'])
                ['_norm_score'].max()
                .reset_index()
            )

            # Step 2: sum each method's max score across methods and count how
            # many distinct methods placed this label in their top-K.
            # Sum > single-method max when both agree → amplified consensus
            # signal.  EnRecord counts unique methods, not raw rows, so
            # Consensus fires whenever ≥2 methods endorse the same label —
            # even when it is not each method's single top pick.
            ml_results = (
                per_method.groupby(attr_key_cols + [attr_col])
                .agg(
                    EnRecord=('method', 'count'),
                    BestScore=('_norm_score', 'sum'),
                    ML_Method=('method', _join_sorted_methods),
                )
                .reset_index()
                .sort_values(
                    attr_key_cols + ['EnRecord', 'BestScore'],
                    ascending=[True] * len(attr_key_cols) + [False, False],
                )
            )
            ml_results['EnRank'] = ml_results.groupby(attr_key_cols).cumcount(ascending=True) + 1
            ml_results['MLTier'] = np.where(ml_results['EnRecord'] > 1, 'Consensus', 'Single')
            ml_results = ml_results[ml_results['EnRank'] == 1].copy()
            # Normalise BestScore relative to the best prediction for this
            # attribute so the top suggestion always reads as 100 and others
            # scale proportionally.  Dividing by a fixed theoretical max (2.0)
            # produced near-zero values in practice because BM25 prob_score is
            # globally min-max scaled (most products score well below the peak)
            # and XGB predict_proba spreads mass across many label classes.
            _max_bs = ml_results['BestScore'].max()
            ml_results['ML Score'] = (
                (ml_results['BestScore'] / _max_bs * 100).round(1)
                if _max_bs > 0 else 0.0
            )
            ml_results = (
                ml_results
                .drop(columns=['BestScore'])
                .rename(columns={attr_col: f'ML{attr_col}', 'ML_Method': 'ML Method'})
            )

        # ── Merge ML predictions into Lookup ──────────────────────────────
        # Normalise join keys in both tables to the same case/format before merging.
        for key_col in attr_key_cols:
            lookup_df[key_col] = lookup_df[key_col].astype(str).str.strip().str.upper()
        for key_col in attr_key_cols:
            ml_results[key_col] = ml_results[key_col].astype(str).str.strip().str.upper()
        lookup_df = pd.merge(lookup_df, ml_results, on=attr_key_cols, how='left')

        # Rank=1 is the highest-confidence recommendation per key combo.
        lookup_df['score'] = lookup_df['score'].fillna(0).round(0).astype(int)
        lookup_df['Rank']  = (
            lookup_df.groupby(attr_key_cols)['score']
            .rank(method='dense', ascending=False)
            .astype(int)
        )

        # ── ML vs Lookup agreement flag ───────────────────────────────────
        ml_col               = f'ML{attr_col}'
        ml_str               = lookup_df[ml_col].astype(str).str.strip()
        has_ml_prediction    = ~ml_str.str.lower().isin(['', 'nan', 'none'])
        ml_and_lookup_agree  = ml_str.str.lower() == lookup_df[attr_col].astype(str).str.strip().str.lower()

        lookup_df['MatchFlag'] = np.where(ml_and_lookup_agree, 1, 0)

        # Explain why ML is absent rather than leaving the cell blank —
        # helps analysts distinguish intentional suppression from a genuine
        # no-match so they know whether to investigate or trust the lookup.
        # 'N/A — numeric'  : attribute was numeric-suppressed (no ML run)
        # 'No match found' : ML ran (TEXT or VOCAB path) but returned nothing
        #                    for this key — lookup suggestion stands alone.
        no_ml_label = 'N/A — numeric' if attr_type == 'NUMERIC' else 'No match found'
        lookup_df['ML Matches Lookup'] = np.where(
            ~has_ml_prediction,
            no_ml_label,
            np.where(ml_and_lookup_agree, 'Yes', 'No'),
        )

        # ── Merge flag table ──────────────────────────────────────────────
        lookup_df[attr_key_cols]            = lookup_df[attr_key_cols].astype(str)
        split_parent_df[attr_key_cols]      = split_parent_df[attr_key_cols].astype(str)
        lookup_df = pd.merge(lookup_df, split_parent_df, on=attr_key_cols, how='left')

        # ── QC Priority ───────────────────────────────────────────────────
        # LOW    → safe to skip.  Exact (score 100) lookup match — the historical
        #          mapping is authoritative, so it's trusted even when the
        #          (generalising) ML disagrees.  ML's alternative is still kept
        #          in the 'ML Matches Lookup' column for optional spot-checking.
        # MEDIUM → worth a glance.  Strong fuzzy match (>=80) confirmed by ML.
        # HIGH   → needs review.  Everything else (weak/no match, or a fuzzy
        #          match the ML doesn't confirm).
        is_exact_match  = lookup_df['score'] >= 100
        is_strong_match = lookup_df['score'] >= 80
        agrees          = lookup_df['MatchFlag'] == 1

        lookup_df['QC Priority'] = np.select(
            [
                is_exact_match,                 # exact lookup match → trusted (LOW)
                is_strong_match & agrees,       # strong fuzzy + ML confirms → MEDIUM
            ],
            ['LOW', 'MEDIUM'],
            default='HIGH',
        )

        # ── Per-row analyst notes ─────────────────────────────────────────
        # Sparse — only written where QC Priority alone doesn't explain the
        # situation.  Notes are written for the analyst, not the developer:
        # use plain English that explains WHY, not just WHAT.
        note = pd.Series('', index=lookup_df.index)

        if skip_ml:
            if attr_type == 'NUMERIC':
                note[:] = (
                    'Numeric/range attribute — ML not applied. '
                    'Range values (e.g. "16 OZ", "22-24 CT") carry no text signal '
                    'a classifier can use. Lookup suggestion is the correct source.'
                )
            elif not vocab_predictor_active:
                note[:] = (
                    'Fixed-list attribute (analyst-flagged) — '
                    'no label match found. Verify the Lookup suggestion manually.'
                )
        else:
            ml_prediction_missing = ~has_ml_prediction
            # Split score=0 rows into two cases:
            # - coverage_gap: blank attribute value — Phase 1 found no match AND
            #   no catch-all bucket exists.  Analyst MUST fill before Phase 2.
            # - hit_fallback_bucket: non-blank value — AO-style catch-all was used.
            is_coverage_gap     = (lookup_df['score'] == 0) & (
                lookup_df[attr_col].fillna('').str.strip() == ''
            )
            hit_fallback_bucket = (lookup_df['score'] == 0) & ~is_coverage_gap
            is_fuzzy_match      = (lookup_df['score'] > 0) & ~is_exact_match

            # Coverage gap — no historical record AND no catch-all fallback label.
            # Phase 2 will leave this attribute blank unless the analyst assigns a
            # value here first.  This is the only case where analyst action is
            # mandatory before running Phase 2.
            note[is_coverage_gap] = (
                'NOT MAPPED — no historical match or fallback label. '
                'Assign a value before running Phase 2.'
            )

            # Genuinely new / uncoded product — not in the training database.
            # A low ML Score here is correct and expected: the model has no
            # historical pattern to generalise from for this key combination.
            # This is not a model failure — it reflects real data novelty.
            note[hit_fallback_bucket & has_ml_prediction] = (
                'New / uncoded value — no historical match exists for this product. '
                'Lookup has assigned the catch-all fallback label. '
                'The ML suggestion is drawn from the nearest training pattern and '
                'may not apply here — analyst judgement required.'
            )
            note[hit_fallback_bucket & ~has_ml_prediction] = (
                'New / uncoded value — no historical match exists for this product. '
                'Lookup has assigned the catch-all fallback label. '
                'ML has no usable training pattern for this key combination.'
            )

            # Fuzzy match where ML and Lookup disagree — two independent signals
            # pointing in different directions.  Check the Lookup score and the
            # ML Score column: the higher-confidence source is likely correct.
            note[~hit_fallback_bucket & is_fuzzy_match & has_ml_prediction & ~agrees] = (
                'Warrants analyst spot-check.'
            )

            # ML returned nothing for a non-exact match — model could not identify
            # a reliable candidate, so the Lookup suggestion stands alone.
            note[~hit_fallback_bucket & ml_prediction_missing & ~is_exact_match] = (
                'No ML suggestion — insufficient training coverage for this key '
                'combination. Verify the Lookup suggestion.'
            )

        lookup_df['Note'] = note

        type_label = _ML_TYPE_CONFIG.get(attr_type, _ML_TYPE_DEFAULT)
        if auto_detected:
            type_label = f'[auto] {type_label}'

        # Break summary into exact / fuzzy / fallback so the log is unambiguous.
        exact_rows    = lookup_df['score'] == 100
        fuzzy_rows    = (lookup_df['score'] > 0) & ~is_exact_match
        fallback_rows = lookup_df['score'] == 0
        exact_match_count = int(lookup_df[exact_rows]['MatchFlag'].sum())
        exact_total       = int(exact_rows.sum())
        fuzzy_match_count = int(lookup_df[fuzzy_rows]['MatchFlag'].sum())
        fuzzy_total       = int(fuzzy_rows.sum())
        fallback_count    = int(fallback_rows.sum())
        summary_parts     = [f'exact {exact_match_count}/{exact_total}', f'fuzzy {fuzzy_match_count}/{fuzzy_total}']
        if fallback_count:
            summary_parts.append(f'{fallback_count} new (fallback)')
        if has_ml_prediction.any():
            # ML ran for this attribute — report ML-vs-Lookup agreement.
            print(f"  Done     {attr_col}: ML agree — {' | '.join(summary_parts)}  [{type_label}]")
        else:
            # No ML ran (routed to Lookup / numeric-suppressed) — a "ML agree 0/N"
            # line here is misleading, so report the lookup outcome instead.
            _newp = f', {fallback_count} new' if fallback_count else ''
            print(f"  Done     {attr_col}: resolved by lookup — no ML applied "
                  f"({exact_total} exact, {fuzzy_total} fuzzy{_newp})  [{type_label}]")

        # Only include Note column when at least one row has a note.
        note_cols = ['Note'] if note.str.strip().any() else []

        ml_method_col_list = ['ML Method'] if 'ML Method' in lookup_df.columns else []
        ml_score_col_list  = ['ML Score']  if 'ML Score'  in lookup_df.columns else []
        dict_ensemble[f'Final_{attr_col}_lkp'] = lookup_df[
            attr_key_cols + [attr_col, ml_col] + ml_method_col_list +
            ['score', 'Rank', 'QC Priority', 'ML Matches Lookup'] +
            ml_score_col_list +
            note_cols
        ]

    return dict_ensemble
