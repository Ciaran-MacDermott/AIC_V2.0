#!/usr/bin/env python
# coding: utf-8
"""
TextMatch — BM25 text retrieval predictor  (Phase 1, Step 2a).

BM25 is a ranking algorithm from information retrieval — the same family of
ideas that powers search engines.  Given a query (a new product's key column
text) and a corpus (all historical key text grouped by label), it scores how
well the query matches each label and returns the top hit.

It works well here because product attribute labels are closely tied to the
words in the product description.  A product with "DARK ROAST" in its name
should score highly against the DARK ROAST training corpus.

What it does
------------
  1. Builds a training corpus from the historical FINAL data.  All key column
     text associated with each label is concatenated, deduplicated, and
     tokenised.  This gives BM25 one document per label to score against.

  2. Builds a query from each new flat-file product by concatenating its key
     column values and tokenising them the same way.

  3. Scores every query against every label document using BM25Plus and takes
     the top result as the prediction.

  4. BM25 raw scores have no common unit across attributes — a score of 5.2
     for BRAND means something completely different to a score of 5.2 for
     FLAVOUR.  So scores are min-max scaled per attribute independently to
     bring them into the 0–1 range before being passed to Ensemble.

Results are stored in recom_dict as 'BM25_{attrG}' and combined with the
XGBoost predictions in Ensemble.

Code style
----------
Functions are written to be read straight through.  Steps are broken into named
variables rather than chained.  If something is not immediately obvious from the
code it has a comment.  Keep it that way.
"""

import re
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import nltk
from nltk.corpus import stopwords
from rank_bm25 import BM25Plus
from sklearn.preprocessing import MinMaxScaler

from ml_package import routing

# Number of candidate labels BM25 returns per product.
# Score-level fusion in Ensemble sums each method's normalised scores across
# all K candidates before taking argmax, so agreement on a label that appears
# in both methods' top-K amplifies its combined score even when it isn't each
# method's single top pick.
_BM25_TOP_K = 3

nltk.download('punkt',     quiet=True)
nltk.download('punkt_tab', quiet=True)
nltk.download('stopwords', quiet=True)

# ── Shared stopword list ──────────────────────────────────────────────────────
# Merged from English NLTK stopwords plus domain-specific noise tokens.
# 'chocolate' and 'original' are intentionally excluded — they are
# discriminating features (e.g. chocolate vs vanilla flavour) and their
# removal was confirmed to degrade BM25 accuracy in benchmarking.
_DOMAIN_STOPWORDS = [
    'ss', 'nan', 'unknown', 'undefined', 'company', 'category', 'llc', 'inc', 'ltd',
    'to', 'oz', 'lt', 'ct', '', 'value', 'not', 'available', 'key', 'missing',
    'label', 'may', 'great', 'from', 'of', 'for', 'null', 'nav', 'card', 'mix', 'nut',
    '&', 'kit', 'sauce', 'dish', 'cup', 'bx', 'envlp', 'can', 'bag', 'btl',
    'rfg', 'cnstr', 'unf', '*', '+', 'a', 'abc', '- .', '.', '-',
    'the', '/', "'", '(', ')', ',', '..', '....',
]
_STOPWORDS = frozenset(set(stopwords.words('english')) | set(_DOMAIN_STOPWORDS))

# ── Auto-NUMERIC detection ────────────────────────────────────────────────────
# Mirrors the logic in Ensemble._infer_attr_type so BM25 skips attributes
# Ensemble will route through Lookup-only anyway, avoiding wasted computation
# and spurious predictions for range-value attributes.
_NUMERIC_RE        = re.compile(r'^\s*[\d#][\d\s\.\-\/]*', re.IGNORECASE)
_NUMERIC_THRESHOLD = 0.60
_NULL_STRS         = {'', 'nan', 'none', 'missing', 'null', 'na',
                      'NaN', 'NAN', 'NONE', 'MISSING', 'NULL', 'NA',
                      'null value', 'NULL VALUE'}


def _is_numeric_attr(attr_key_cols: list, data_df: pd.DataFrame) -> bool:
    """Return True if >60% of unique non-null key-column values are numeric/range."""
    for col in attr_key_cols:
        if col not in data_df.columns:
            continue
        stripped_values = data_df[col].dropna().astype(str).str.strip()
        is_null_string = stripped_values.str.lower().isin(_NULL_STRS)
        non_null_values = stripped_values[~is_null_string].unique()
        if len(non_null_values) == 0:
            continue
        numeric_match_count = sum(bool(_NUMERIC_RE.match(v)) for v in non_null_values)
        numeric_fraction = numeric_match_count / len(non_null_values)
        if numeric_fraction >= _NUMERIC_THRESHOLD:
            return True
    return False


def _build_training_corpus(history_df: pd.DataFrame, meta_df: pd.DataFrame,
                            mdm_col: str) -> pd.DataFrame:
    """
    Build a tokenised BM25 training corpus from historical FINAL data.

    Groups all key column values by label, deduplicates tokens, applies
    stopword removal, and tokenises with NLTK.  Returns a DataFrame indexed
    by label with one '_tokenize' column per key column group.
    """
    attr_key_cols = list(meta_df.loc[meta_df['Attribute Group name'] == mdm_col,
                                     'Attribute Name in MDM'])
    df = history_df[attr_key_cols + [mdm_col]].copy()

    # symmetric_difference([mdm_col]) gives all columns except the label column.
    key_cols_only = df.columns.symmetric_difference([mdm_col])
    for col in key_cols_only:
        df[col] = df[col].replace({'&': ''}, regex=True)
    df.fillna('unknown', inplace=True)
    df.replace(re.compile(r'^\s*null\s+value\s*$', re.IGNORECASE), 'unknown', inplace=True)

    df = df.groupby([mdm_col]).agg(lambda x: ' '.join(x))
    df = df.apply(lambda x: x.astype(str).str.lower())

    # Deduplicate tokens within each cell so repeated words don't inflate BM25 IDF.
    for col in df.columns:
        df[col] = df[col].apply(lambda x: ' '.join(dict.fromkeys(x.split())))

    combined_col = 'X_' + mdm_col
    df[combined_col] = df.astype(str).apply(' '.join, axis=1)
    # Same dedup on the combined column after concatenating all key columns together.
    df[combined_col] = df[combined_col].apply(lambda x: ' '.join(sorted(set(x.split()))))

    for i, col in enumerate(attr_key_cols):
        df = df.rename(columns={col: f'X{i + 1}_{mdm_col}'})

    # Two-step feature build per column:
    #   _text      → stopword-filtered string  (human-readable, used for tokenisation)
    #   _tokenize  → NLTK word token list      (the actual BM25Plus input format)
    # Only the _tokenize columns are returned; _text is an intermediate step.
    for col in list(df.columns):
        df[col + '_text'] = df[col].apply(
            lambda x: ' '.join(w for w in x.split() if w not in _STOPWORDS)
        )
    # word_tokenize splits on punctuation as well as whitespace, which is
    # the token format BM25Plus expects.
    for col in [c for c in df.columns if 'text' in c]:
        df[col + '_tokenize'] = df[col].apply(nltk.word_tokenize)

    return df[[c for c in df.columns if '_tokenize' in c]]


def _build_query_corpus(flat_file_df: pd.DataFrame, meta_df: pd.DataFrame,
                         mdm_col: str) -> pd.DataFrame:
    """
    Build a tokenised BM25 query corpus from the flat-file (new products).

    Concatenates all key column values per product row, removes stopwords,
    and tokenises.  Returns a DataFrame with ITEM_DIM_KEY, key columns,
    and a 'test_tokenize' column.
    """
    attr_key_cols = list(meta_df.loc[meta_df['Attribute Group name'] == mdm_col,
                                     'Attribute Name in MDM'])
    df = flat_file_df[attr_key_cols + ['ITEM_DIM_KEY']].copy()
    df = df.apply(lambda x: x.astype(str).str.lower())
    df.fillna('unknown', inplace=True)
    df.replace(re.compile(r'^\s*null\s+value\s*$', re.IGNORECASE), 'unknown', inplace=True)

    df['test']          = df[attr_key_cols].apply(lambda row: ' '.join(row.values.astype(str)), axis=1)
    df['test']          = df['test'].replace({'&': ''}, regex=True)
    df['test_text']     = df['test'].apply(lambda x: ' '.join(w for w in x.split() if w not in _STOPWORDS))
    df['test_tokenize'] = df['test_text'].apply(nltk.word_tokenize)
    df['ITEM_DIM_KEY']  = df['ITEM_DIM_KEY'].astype(str)

    data = pd.merge(
        flat_file_df[['ITEM_DIM_KEY'] + attr_key_cols].astype({'ITEM_DIM_KEY': str}),
        df[['ITEM_DIM_KEY', 'test_tokenize']],
        on='ITEM_DIM_KEY', how='left',
    )
    return data[[c for c in data.columns if '_tokenize' in c] + ['ITEM_DIM_KEY'] + attr_key_cols]


def _process_one_bm25_attr(mdm_col: str, meta_df: pd.DataFrame,
                            history_df: pd.DataFrame,
                            flat_file_df: pd.DataFrame):
    """Process a single attribute for BM25 prediction. Returns DataFrame or None."""
    attr_key_cols = list(meta_df.loc[meta_df['Attribute Group name'] == mdm_col,
                                     'Attribute Name in MDM'])

    meta_type_vals = (
        meta_df.loc[meta_df['Attribute Group name'] == mdm_col, 'Type']
        .dropna().astype(str).str.strip().str.upper()
        .pipe(lambda s: s[~s.isin(_NULL_STRS)])
    )
    if len(meta_type_vals):
        type_val  = meta_type_vals.iloc[0]
        is_vocab  = type_val in ('VOCAB', 'DERIVED', 'CATEGORICAL')
        type_label = 'short fixed-list' if is_vocab else 'numeric/range'
        print(f"  BM25     {mdm_col}: skipped — analyst-marked as {type_label}")
        return None
    if _is_numeric_attr(attr_key_cols, history_df):
        print(f"  BM25     {mdm_col}: skipped — auto-detected as numeric/range")
        return None

    # Routing: skip BM25 for identity/derived composites (openness ~ 1) and
    # pathological label spaces — Lookup carries them, and BM25's dense
    # [n_products x n_labels] score matrix is a memory hazard on huge label sets.
    skip, _ = routing.skip_learned_methods(history_df, attr_key_cols, mdm_col)
    if skip:
        print(f"  BM25     {mdm_col}: skipped — resolved by lookup (no modelling needed)")
        return None

    training_corpus = _build_training_corpus(history_df, meta_df, mdm_col)
    training_corpus = training_corpus.reset_index()
    training_corpus = training_corpus.rename(columns={
        mdm_col:                            'predicted',
        f'X_{mdm_col}_text_tokenize':       'corpus',
    })
    training_corpus = training_corpus[training_corpus['corpus'].map(len) > 0]
    # Guard: all training documents reduced to empty token lists after stopword
    # removal — BM25Plus([]) would silently return zero scores for every query.
    if training_corpus.empty:
        print(f"  BM25     {mdm_col}: skipped — training corpus empty after tokenisation "
              f"(all historical key values reduced to stop words or punctuation; "
              f"review input data for {mdm_col})")
        return None

    query_corpus = _build_query_corpus(flat_file_df, meta_df, mdm_col)
    if query_corpus.empty:
        print(f"  BM25     {mdm_col}: skipped — no flat-file products have non-empty key values to score")
        return None

    print(f"  BM25     {mdm_col}: {len(query_corpus)} products")

    query_corpus = (
        query_corpus
        .rename(columns={'test_tokenize': 'corpus'})
        .reset_index(drop=True)
    )
    # Deduplicate tokens in each query (corpus column holds token lists at this point).
    query_corpus['corpus'] = query_corpus['corpus'].apply(lambda x: ' '.join(sorted(set(x))))

    try:
        tokenized_corpus = training_corpus['corpus'].tolist()
        pred_list        = training_corpus['predicted'].tolist()
        bm25             = BM25Plus(tokenized_corpus)

        # Vectorised score matrix: rows = products, cols = training labels.
        queries    = query_corpus['corpus'].str.split().tolist()
        scores_mat = np.abs(np.vstack([bm25.get_scores(q) for q in queries]))
        pred_arr   = np.array(pred_list)

        # Return top-K candidates per product rather than top-1.
        # Ensemble fuses BM25 and XGB by summing each method's max normalised
        # score per label, so a label appearing in both methods' top-K receives
        # a combined score even when it is not each method's single best pick.
        k            = min(_BM25_TOP_K, scores_mat.shape[1])
        top_k_idx    = np.argsort(-scores_mat, axis=1)[:, :k]
        top_k_scores = np.take_along_axis(scores_mat, top_k_idx, axis=1)

        # Expand: one row per (product, candidate) pair.
        repeated              = query_corpus.loc[
            query_corpus.index.repeat(k)
        ].reset_index(drop=True)
        repeated['predicted'] = pred_arr[top_k_idx.ravel()]
        repeated['max_score'] = top_k_scores.ravel()
        repeated['document']  = mdm_col

        return repeated.fillna('')
    except Exception as exc:
        print(f"  BM25     {mdm_col}: ERROR during scoring — {exc}")
        return None


def _run_bm25(meta_df: pd.DataFrame, history_df: pd.DataFrame,
              flat_file_df: pd.DataFrame) -> pd.DataFrame:
    """
    Run BM25 prediction for all eligible attributes in parallel and return a
    combined result DataFrame with columns: corpus, ITEM_DIM_KEY, predicted,
    max_score, document, plus per-attribute key columns.

    Attributes flagged as NUMERIC or VOCAB in META are skipped — Ensemble
    routes these through Lookup-only or vocab matching respectively.
    Attributes are processed in parallel across 4 workers (numpy releases the
    GIL so threading is effective here).
    """
    attrs = meta_df['Attribute Group name'].unique().tolist()
    results_frames = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_process_one_bm25_attr, col, meta_df, history_df, flat_file_df)
            for col in attrs
        ]
        for future in futures:
            result = future.result()
            if result is not None:
                results_frames.append(result)

    all_attr_cols = list(meta_df['Attribute Name in MDM'].unique())
    empty_frame   = pd.DataFrame(
        columns=['corpus', 'ITEM_DIM_KEY', 'score', 'predicted', 'max_score', 'document'] + all_attr_cols
    )
    return pd.concat([empty_frame] + results_frames, ignore_index=True)


def runTextMatch(meta_df: pd.DataFrame, history_df: pd.DataFrame,
                 flat_file_df: pd.DataFrame, recom_dict: dict) -> dict:
    """
    Run BM25 predictions for all eligible attributes and store results in
    recom_dict as 'BM25_{attrG}'.

    Scores are min-max scaled per attribute independently — a BM25 score
    of 0.5 for BRAND is on a completely different absolute scale to 0.5 for
    FLAVOR, so they must not be normalised together.

    Parameters
    ----------
    meta_df      : META sheet DataFrame.
    history_df   : Historical FINAL data (training reference).
    flat_file_df : New products flat file (query set).
    recom_dict   : Accumulator dict; BM25 results added as 'BM25_{attrG}'.

    Returns
    -------
    recom_dict (updated in-place and returned).
    """
    bm25_results = _run_bm25(meta_df, history_df, flat_file_df)
    bm25_results['ITEM_DIM_KEY'] = pd.to_numeric(bm25_results['ITEM_DIM_KEY'])

    # Scale per attribute independently — BM25 raw scores have no common unit
    # across attributes (BRAND scores are on a completely different scale to
    # FLAVOUR scores), so each attribute's scores are min-max scaled to [0, 1]
    # separately before being stored as prob_score.
    # Note: fit_transform inside groupby.transform is intentional here — each
    # group (attribute) needs its own min/max, not a global fit.
    scaler = MinMaxScaler()
    bm25_results['prob_score'] = (
        bm25_results.groupby('document')['max_score']
        .transform(lambda x: scaler.fit_transform(x.values.reshape(-1, 1)).ravel())
    )

    for mdm_col in meta_df['Attribute Group name'].unique():
        attr_key_cols = list(meta_df.loc[meta_df['Attribute Group name'] == mdm_col,
                                         'Attribute Name in MDM'])
        recom_dict['BM25_' + mdm_col] = (
            bm25_results[bm25_results['document'] == mdm_col][
                attr_key_cols + ['predicted', 'max_score', 'prob_score']
            ].rename(columns={'max_score': 'score', 'predicted': mdm_col})
        )

    return recom_dict
