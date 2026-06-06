#!/usr/bin/env python
# coding: utf-8
"""
RandomForest_XGB — XGBoost TF-IDF text classifier  (Phase 1, Step 2b).

Where BM25 ranks by keyword relevance, XGBoost learns patterns.  It is trained
on TF-IDF features built from the historical data — bigrams of product key text
weighted by how distinctive they are across the label vocabulary.  It then
predicts the most likely label for each new product.

The two approaches complement each other.  BM25 is strong when the label text
overlaps directly with the product description.  XGBoost can pick up on
patterns that are not obvious from individual words.  Ensemble combines both.

What it does
------------
  1. For each TEXT-type attribute, builds a TF-IDF feature matrix from the
     historical key column text.  Word bigrams are used (not just single words)
     because compound terms like "dark chocolate" or "reduced fat" carry more
     signal than either word alone.

  2. Optionally augments the training data by creating masked copies of each
     row where a random subset of key columns is replaced with "missing".  This
     teaches the model to make predictions even when some columns are blank,
     which is common for genuinely new products in the flat file.

  3. Trains an XGBoost classifier on the augmented features and predicts the
     top label plus confidence score for each new product.

  4. Results are grouped by unique key combination so the output is one row per
     distinct product type rather than one row per individual product.

Results are stored in the returned dict as 'XGB_{attrG}' and combined with
the BM25 predictions in Ensemble.

Model configuration
-------------------
n_estimators=150, learning_rate=0.1, max_depth=6.  These were chosen to give
good accuracy on short product text without overfitting.  n_jobs=2 per model
because four attributes run in parallel via ThreadPoolExecutor, keeping total
thread usage at 4x2=8 which matches the available vCPUs.

Code style
----------
Functions are written to be read straight through.  Steps are broken into named
variables rather than chained.  If something is not immediately obvious from the
code it has a comment.  Keep it that way.
"""

import re
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import nltk
from nltk.corpus import stopwords
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn import preprocessing
import xgboost as xgb

warnings.filterwarnings('ignore')
nltk.download('punkt',     quiet=True)
nltk.download('punkt_tab', quiet=True)
nltk.download('stopwords', quiet=True)


# ── Auto-NUMERIC detection (mirrors Ensemble._infer_attr_type) ────────────────
_NUMERIC_RE        = re.compile(r'^\s*[\d#][\d\s\.\-\/]*', re.IGNORECASE)
_NUMERIC_THRESHOLD = 0.60
_NULL_STRS         = {'', 'nan', 'none', 'missing', 'null', 'na', 'null value'}

# ── Stopwords ─────────────────────────────────────────────────────────────────
# 'no' and 'to' are kept (removed from NLTK stopwords) because they carry
# product-attribute signal in this domain.
# NOTE: 'chocolate', 'original', etc. intentionally excluded from removals —
# these ARE discriminating features for product attribute classification.
_DOMAIN_STOPWORDS = [
    'ss', 'unknown', 'undefined', 'company', 'category', 'llc', 'inc', 'ltd',
    'to', 'oz', 'lt', 'ct', '', 'value', 'not', 'available', 'key', 'label',
    'may', 'great', 'from', 'of', 'for', 'null', 'nav', 'card', 'mix', 'nut',
    '&', 'kit', 'sauce', 'dish', 'cup', 'bx', 'envlp', 'can', 'bag', 'btl',
    'rfg', 'cnstr', 'unf', '*', '+', '/', 'abc', 'a', '-',
]
_NOT_STOPWORDS = {'no', 'to'}
_STOPWORDS = frozenset(
    (set(stopwords.words('english')) | set(_DOMAIN_STOPWORDS)) - _NOT_STOPWORDS
)

# ── Training-data augmentation ────────────────────────────────────────────────
# For each training row, N masked copies are created where a random subset of
# key columns is set to 'missing' while one randomly-chosen anchor column is
# always kept.  Teaches XGB to predict from partial key-column combinations —
# useful when new flat-file products arrive with some key columns unfilled.
_AUG_N_COPIES = 3   # total training rows become (1 + N) × original
_aug_rng = np.random.default_rng(42)

# ── Model configuration ───────────────────────────────────────────────────────
# Word n-grams (1-2): bigrams capture compound product terms like
# "dark chocolate" vs "milk chocolate".
# min_df=1 keeps rare tokens — product catalogs are small, so rare terms
# are still signal.  sublinear_tf dampens frequency dominance of common tokens.
_tfidf = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True, analyzer='word')

# n_estimators=150 + learning_rate=0.1: more trees with smaller steps vs
#   default (100, 0.3).
# Number of candidate labels XGB returns per product — mirrors _BM25_TOP_K.
_XGB_TOP_K = 3

# High-cardinality cutoff. XGBoost multiclass cost scales with the class count
# (it trains ~ n_classes * n_estimators trees), so a label column with thousands
# of distinct values dominates the stage for little gain. Above this many distinct
# label values XGB is skipped and the attribute is carried by Lookup + BM25.
# Set to 500 -- between the TH dataset's 263 and 2,795 cardinality clusters -- so it
# skips the Franchise_Packtype_RPTG identity field (~2,795 values, the ~40-min fit)
# but KEEPS XGB for Tool_Franchise_TH (~263): a 2026-06-06 holdout test showed XGB
# beats BM25 by ~92pp on that attribute's genuinely-new (Lookup-miss) products.
_XGB_MAX_CLASSES = 500

# subsample=0.8: row subsampling reduces overfitting.
# colsample_bytree=0.4: column subsampling for sparse TF-IDF features.
# tree_method='hist': faster histogram splits on sparse matrices.
# n_jobs=2: 4 attributes run in parallel × 2 internal threads = 8 vCPUs fully used.
_XGB_PARAMS = dict(
    n_estimators=150,
    learning_rate=0.1,
    max_depth=6,
    subsample=0.8,
    colsample_bytree=0.4,
    min_child_weight=1,
    tree_method='hist',
    n_jobs=2,
    random_state=42,
    verbosity=0,
)


def _is_numeric_attr(attr_key_cols: list, data_df: pd.DataFrame) -> bool:
    """Return True if >60% of unique non-null key-column values are numeric/range."""
    for col in attr_key_cols:
        if col not in data_df.columns:
            continue
        vals = (
            data_df[col].dropna().astype(str).str.strip()
            .pipe(lambda s: s[~s.str.lower().isin(_NULL_STRS)])
            .unique()
        )
        if len(vals) == 0:
            continue
        numeric_match_count = sum(bool(_NUMERIC_RE.match(v)) for v in vals)
        numeric_fraction = numeric_match_count / len(vals)
        if numeric_fraction >= _NUMERIC_THRESHOLD:
            return True
    return False


def _augment_training(df: pd.DataFrame, attr_key_cols: list) -> pd.DataFrame:
    """
    Return df plus _AUG_N_COPIES masked copies for training-data augmentation.

    Each copy masks a random subset of key columns to 'missing' (p=0.5 per
    column per row), while one randomly-chosen anchor column per row is always
    preserved.  No-op when attr_key_cols has only one column.
    """
    available_cols = [c for c in attr_key_cols if c in df.columns]
    if len(available_cols) < 2:
        return df

    copies = [df]
    for _ in range(_AUG_N_COPIES):
        aug             = df.copy()
        anchor_col_index = _aug_rng.integers(0, len(available_cols), size=len(aug))
        for col_index, col in enumerate(available_cols):
            mask = (_aug_rng.random(len(aug)) < 0.5) & (anchor_col_index != col_index)
            aug.loc[mask, col] = 'missing'
        copies.append(aug)
    return pd.concat(copies, ignore_index=True)


def _remove_duplicate_words(text: str) -> str:
    """Remove duplicate words from a string while preserving order."""
    words = text.split()
    return ' '.join(dict.fromkeys(words))


def _build_features(train_df: pd.DataFrame, test_df: pd.DataFrame):
    """
    Concatenate all key columns into a single feature string per row,
    apply stopword removal and deduplication, and fit-transform TF-IDF
    on training data / transform test data.

    Returns (train_df_with_ftr, test_df_with_ftr).
    """
    train_df['ftr'] = train_df.iloc[:, :-1].astype(str).apply(' '.join, axis=1)
    test_df['ftr']  = test_df.astype(str).apply(' '.join, axis=1)

    def _clean(text_series):
        return (
            text_series.str.lower().astype(str)
            .apply(_remove_duplicate_words)
            .apply(lambda x: ' '.join(w for w in x.split() if w not in _STOPWORDS))
        )

    train_df['ftr'] = _clean(train_df['ftr'])
    test_df['ftr']  = _clean(test_df['ftr'])
    return train_df, test_df


def _process_one_xgb_attr(mdm_col: str, meta_df: pd.DataFrame,
                           history_df: pd.DataFrame,
                           flat_file_df: pd.DataFrame):
    """
    Train and predict for a single attribute. Returns (key, DataFrame) or None.
    Creates fresh TF-IDF and XGBoost instances so parallel calls don't share state.
    """
    attr_key_cols = list(meta_df.loc[meta_df['Attribute Group name'] == mdm_col,
                                     'Attribute Name in MDM'])

    meta_type_vals = (
        meta_df.loc[meta_df['Attribute Group name'] == mdm_col, 'Type']
        .dropna().astype(str).str.strip().str.upper()
        .pipe(lambda s: s[~s.isin({'', 'NAN', 'NONE', 'NA'})])
    )
    if len(meta_type_vals):
        type_val   = meta_type_vals.iloc[0]
        is_vocab   = type_val in ('VOCAB', 'DERIVED', 'CATEGORICAL')
        type_label = 'short fixed-list' if is_vocab else 'numeric/range'
        print(f"  XGB      {mdm_col}: skipped — analyst-marked as {type_label}")
        return None
    if _is_numeric_attr(attr_key_cols, history_df):
        print(f"  XGB      {mdm_col}: skipped — auto-detected as numeric/range")
        return None

    try:
        # High-cardinality guard: skip XGB when the label column has more distinct
        # values than _XGB_MAX_CLASSES — Lookup + BM25 carry the attribute instead.
        n_distinct = int(history_df.loc[history_df[mdm_col].notna(), mdm_col].nunique())
        if n_distinct > _XGB_MAX_CLASSES:
            print(f"  XGB      {mdm_col}: skipped — {n_distinct} distinct values "
                  f"exceeds cap of {_XGB_MAX_CLASSES} (routed via Lookup + BM25)")
            return None

        train_df = (
            history_df[attr_key_cols + [mdm_col]]
            .replace(r'(OZ|LB)', ' ', regex=True)
            .dropna(subset=[mdm_col])
        )
        train_df.fillna('missing', inplace=True)
        # "NULL VALUE" is a Circana sentinel for unfilled cells. Both tokens
        # are stopwords, so the feature string collapses to "" and triggers
        # library warnings.  Normalise to 'missing' before feature building.
        train_df.replace(re.compile(r'^\s*null\s+value\s*$', re.IGNORECASE),
                         'missing', inplace=True)

        train_df = _augment_training(train_df, attr_key_cols)

        test_df = (
            flat_file_df[attr_key_cols]
            .replace(r'(OZ|LB)', ' ', regex=True)
            .copy()
        )
        if test_df.empty:
            return None
        test_df.fillna('missing', inplace=True)
        test_df.replace(re.compile(r'^\s*null\s+value\s*$', re.IGNORECASE),
                        'missing', inplace=True)

        train_df, test_df = _build_features(train_df, test_df)

        # Fresh instances per attribute — required for parallel safety.
        tfidf     = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True, analyzer='word')
        xgb_model = xgb.XGBClassifier(**_XGB_PARAMS)

        train_features = tfidf.fit_transform(train_df['ftr'])
        test_features  = tfidf.transform(test_df['ftr'])

        label_encoder = preprocessing.LabelEncoder()
        label_encoder.fit(train_df[mdm_col])
        print(f"  XGB      {mdm_col}: {len(test_df)} products | {len(label_encoder.classes_)} known values")

        y_encoded = label_encoder.transform(train_df[mdm_col])
        xgb_model.fit(train_features, y_encoded)

        class_labels = label_encoder.inverse_transform(xgb_model.classes_)

        # proba_scores shape: [n_products, n_classes] — one probability per label per product.
        proba_scores = xgb_model.predict_proba(test_features)

        # Top-K candidates — mirrors BM25 so Ensemble can fuse score distributions
        # from both methods rather than comparing only single top picks.
        k           = min(_XGB_TOP_K, proba_scores.shape[1])
        top_indices = np.argsort(-proba_scores, axis=1)[:, :k]
        top_scores  = np.take_along_axis(proba_scores, top_indices, axis=1)
        top_labels  = class_labels[top_indices]

        # Expand test_df to k rows per product, one per candidate label.
        expanded          = test_df.loc[test_df.index.repeat(k)].reset_index(drop=True)
        expanded[mdm_col] = top_labels.ravel()
        expanded['score'] = (top_scores.ravel() * 100).round(2)

        grouped = (
            expanded.groupby(attr_key_cols + [mdm_col, 'score'])['ftr']
            .count()
        )
        grouped = grouped.reset_index(name='Record')
        grouped = grouped.replace('MISSING', '', regex=True)
        grouped['method'] = 'XGB'
        return (f'XGB_{mdm_col}', grouped)

    except Exception as exc:
        print(f"  ERROR: XGB failed for '{mdm_col}': {exc}")
        return None


def runML(flat_file_df: pd.DataFrame, meta_df: pd.DataFrame,
          history_df: pd.DataFrame) -> dict:
    """
    Train and run XGBoost TF-IDF predictions for all eligible attributes in parallel.

    Parameters
    ----------
    flat_file_df : DataFrame
        New products to classify (the flat-file CSV, string-coerced).
    meta_df : DataFrame
        META sheet — defines attribute groups, key columns, and type flags.
    history_df : DataFrame
        Historical FINAL data used as the labelled training set.

    Returns
    -------
    dict mapping 'XGB_{attrG}' → prediction DataFrame with columns:
    attr_key_cols + [attrG, score, Record, method].

    Attributes are processed in parallel across 4 workers; each XGBoost
    instance uses n_jobs=2 so total thread usage stays at 4×2 = 8 vCPUs.
    """
    attrs = meta_df['Attribute Group name'].unique().tolist()
    xgb_results = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_process_one_xgb_attr, col, meta_df, history_df, flat_file_df)
            for col in attrs
        ]
        for future in futures:
            result = future.result()
            if result is not None:
                key, df = result
                xgb_results[key] = df

    return xgb_results
