#!/usr/bin/env python
# coding: utf-8
"""
ML classifier — TF-IDF text classifier  (Phase 1, Step 2b).

For each TEXT-type attribute this builds a TF-IDF feature matrix from the
historical key-column text (word bigrams) and trains a classifier to predict the
most likely label for each new product.  Where BM25 ranks by keyword relevance,
the classifier learns patterns; Ensemble fuses the two.

Classifier backend
------------------
As of 2026-06-06 the backend is **LinearSVC**.  A head-to-head on this project's
sparse TF-IDF features showed it matching or beating the previous XGBoost backend
on accuracy at ~2.5x less compute (linear models are the canonical strong choice
for high-dimensional sparse text; gradient-boosted trees are in their weakest
regime there).  The retired XGBoost configuration is preserved, commented, below
(`_XGB_PARAMS`) for easy revival.  Output is keyed/tagged 'ML' and consumed
unchanged by Ensemble.

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

  3. Trains the classifier on the augmented features and predicts the top label
     plus confidence score for each new product.

  4. Results are grouped by unique key combination so the output is one row per
     distinct product type rather than one row per individual product.

Results are stored in the returned dict as 'ML_{attrG}' and combined with
the BM25 predictions in Ensemble.

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
from sklearn.svm import LinearSVC
from sklearn.calibration import CalibratedClassifierCV
# import xgboost as xgb   # retired in favour of LinearSVC — see _make_classifier()
#                          and the preserved _XGB_PARAMS block below.

from ml_package import routing

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
# always kept.  Teaches the classifier to predict from partial key-column
# combinations — useful when new flat-file products arrive with some columns blank.
_AUG_N_COPIES = 3   # total training rows become (1 + N) × original
_aug_rng = np.random.default_rng(42)

# ── Model configuration ───────────────────────────────────────────────────────
# Word n-grams (1-2): bigrams capture compound product terms like
# "dark chocolate" vs "milk chocolate".
# min_df=1 keeps rare tokens — product catalogs are small, so rare terms
# are still signal.  sublinear_tf dampens frequency dominance of common tokens.
_tfidf = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True, analyzer='word')

# Number of candidate labels the classifier returns per product — mirrors _BM25_TOP_K.
_ML_TOP_K = 3

# Per-attribute routing — whether the classifier runs at all — lives in
# ml_package.routing and is applied in _process_one_xgb_attr below.  It skips
# identity/derived composites (openness ~ 1, e.g. Franchise_Packtype_RPTG ~2,795)
# while keeping genuine classifications (e.g. Tool_Franchise_TH ~263), and
# replaces the earlier blunt class-count cap.  See routing.skip_learned_methods().


def _make_classifier():
    """
    Build a fresh classifier for the learnable TEXT tier (one per attribute so
    parallel calls don't share state).

    Backend: LinearSVC — a 2026-06-06 head-to-head on this project's sparse
    TF-IDF features showed it matching/beating XGBoost on accuracy at ~2.5x less
    compute.  LogReg (one-vs-rest) is a faster, slightly-less-accurate alternative.
    """
    return LinearSVC(max_iter=5000, random_state=42)
    # Speed-first alternative (add: from sklearn.linear_model import LogisticRegression
    # and from sklearn.multiclass import OneVsRestClassifier):
    #   return OneVsRestClassifier(LogisticRegression(solver="liblinear", max_iter=1000))


def _ml_scores(model, features):
    """
    Return an [n_samples, n_classes] confidence matrix aligned to model.classes_.

    Uses predict_proba when the estimator provides it (LogReg / the old XGB);
    otherwise softmaxes LinearSVC's decision_function margins into 0..1.  Either
    way Ensemble's contract holds — it reads the per-row `score` as a 0..1
    confidence (score / 100) regardless of which classifier produced it.
    """
    if hasattr(model, "predict_proba"):
        return model.predict_proba(features)
    margins = model.decision_function(features)
    if margins.ndim == 1:                       # binary problem → two columns
        margins = np.column_stack([-margins, margins])
    margins = margins - margins.max(axis=1, keepdims=True)
    exp = np.exp(margins)
    return exp / exp.sum(axis=1, keepdims=True)


# ── Preserved for reference: the retired XGBoost backend ──────────────────────
# Replaced by LinearSVC (see _make_classifier) on 2026-06-06.  To revive:
# uncomment `import xgboost as xgb` above, uncomment this block, and return
# `xgb.XGBClassifier(**_XGB_PARAMS)` from _make_classifier().
#   n_estimators=150 + learning_rate=0.1: more trees, smaller steps.
#   subsample=0.8 / colsample_bytree=0.4: row / column subsampling for sparse TF-IDF.
#   tree_method='hist': faster histogram splits.  n_jobs=2.
# _XGB_PARAMS = dict(
#     n_estimators=150,
#     learning_rate=0.1,
#     max_depth=6,
#     subsample=0.8,
#     colsample_bytree=0.4,
#     min_child_weight=1,
#     tree_method='hist',
#     n_jobs=2,
#     random_state=42,
#     verbosity=0,
# )


def _calibration_split(y, frac_cal: float = 0.25, seed: int = 42):
    """
    Held-out split for Platt calibration that never drops a class.

    Every class keeps >=1 sample in the fit set (so the base estimator learns all
    of them — important because the minority classes here are genuinely rare);
    classes with >=2 samples also contribute ~frac_cal of their rows to the
    calibration set.  Returns (fit_idx, cal_idx) as int arrays.
    """
    rng = np.random.default_rng(seed)
    y = np.asarray(y)
    fit_idx, cal_idx = [], []
    for c in np.unique(y):
        idx = np.where(y == c)[0]
        rng.shuffle(idx)
        if len(idx) < 2:
            fit_idx.extend(idx.tolist())          # lone sample stays in fit
            continue
        n_cal = min(len(idx) - 1, max(1, int(round(len(idx) * frac_cal))))
        cal_idx.extend(idx[:n_cal].tolist())
        fit_idx.extend(idx[n_cal:].tolist())
    return np.array(fit_idx, dtype=int), np.array(cal_idx, dtype=int)


def _fit_calibrated(train_features, y_encoded):
    """
    Fit LinearSVC and Platt-calibrate its confidence onto a proper probability
    scale.  A bare softmax over hundreds of classes yields a ~0.07 top-1 even
    when the model is right, which drowns ML out of the BM25+ML score fusion and
    floods QC with false MEDIUM flags.  Sigmoid (Platt) calibration on a held-out
    split maps the SVM margins to calibrated probabilities; predict_proba then
    feeds Ensemble unchanged via _ml_scores.

    Robust to genuinely-rare classes: _calibration_split keeps every class in the
    fit set, and we fall back to the uncalibrated LinearSVC if calibration can't
    be fit (too few rows, or only one class in the calibration split).
    """
    fit_i, cal_i = _calibration_split(y_encoded)
    if cal_i.size and len(np.unique(y_encoded[cal_i])) >= 2:
        try:
            base = _make_classifier()
            base.fit(train_features[fit_i], y_encoded[fit_i])
            # Calibrate the prefit base.  sklearn >= 1.6 removed cv='prefit' in
            # favour of FrozenEstimator; older sklearn still uses cv='prefit'.
            # Try the modern path first, fall back for < 1.6.
            try:
                from sklearn.frozen import FrozenEstimator   # sklearn >= 1.6
                calibrated = CalibratedClassifierCV(FrozenEstimator(base), method='sigmoid')
            except ImportError:
                calibrated = CalibratedClassifierCV(base, method='sigmoid', cv='prefit')
            calibrated.fit(train_features[cal_i], y_encoded[cal_i])
            return calibrated
        except Exception:                         # noqa: BLE001 — degrade gracefully
            # Calibration is a best-effort confidence refinement. A genuine
            # failure is worth a brief note (helps diagnose) but not the full
            # traceback — fall through to the uncalibrated classifier.
            print("  ML       confidence calibration unavailable — using raw scores")
    clf = _make_classifier()
    clf.fit(train_features, y_encoded)
    return clf


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
    Creates fresh TF-IDF and classifier instances so parallel calls don't share state.
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
        print(f"  ML       {mdm_col}: skipped — analyst-marked as {type_label}")
        return None
    if _is_numeric_attr(attr_key_cols, history_df):
        print(f"  ML       {mdm_col}: skipped — auto-detected as numeric/range")
        return None

    try:
        # Routing: skip the classifier for identity/derived composites (openness
        # ~ 1) and pathological label spaces — Lookup carries them instead.
        skip, _ = routing.skip_learned_methods(history_df, attr_key_cols, mdm_col)
        if skip:
            print(f"  ML       {mdm_col}: skipped — resolved by lookup (no modelling needed)")
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

        # Fresh TF-IDF per attribute — required for parallel safety.
        tfidf = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True, analyzer='word')

        train_features = tfidf.fit_transform(train_df['ftr'])
        test_features  = tfidf.transform(test_df['ftr'])

        label_encoder = preprocessing.LabelEncoder()
        label_encoder.fit(train_df[mdm_col])
        print(f"  ML       {mdm_col}: {len(test_df)} products | {len(label_encoder.classes_)} known values")

        y_encoded = label_encoder.transform(train_df[mdm_col])
        clf = _fit_calibrated(train_features, y_encoded)

        class_labels = label_encoder.inverse_transform(clf.classes_)

        # Calibrated confidence matrix [n_products, n_classes] — Platt-scaled
        # predict_proba (see _fit_calibrated); _ml_scores reads it unchanged.
        proba_scores = _ml_scores(clf, test_features)

        # Top-K candidates — mirrors BM25 so Ensemble can fuse score distributions
        # from both methods rather than comparing only single top picks.
        k           = min(_ML_TOP_K, proba_scores.shape[1])
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
        grouped['method'] = 'ML'
        return (f'ML_{mdm_col}', grouped)

    except Exception as exc:
        print(f"  ERROR: ML failed for '{mdm_col}': {exc}")
        return None


def runML(flat_file_df: pd.DataFrame, meta_df: pd.DataFrame,
          history_df: pd.DataFrame) -> dict:
    """
    Train and run the TF-IDF classifier for all eligible attributes in parallel.

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
    dict mapping 'ML_{attrG}' → prediction DataFrame with columns:
    attr_key_cols + [attrG, score, Record, method].

    Attributes are processed in parallel across 4 workers; the LinearSVC backend
    is single-threaded per fit, so 4 attributes train concurrently.
    """
    attrs = meta_df['Attribute Group name'].unique().tolist()
    ml_results = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_process_one_xgb_attr, col, meta_df, history_df, flat_file_df)
            for col in attrs
        ]
        for future in futures:
            result = future.result()
            if result is not None:
                key, df = result
                ml_results[key] = df

    return ml_results
