#!/usr/bin/env python
# coding: utf-8
"""
MappingLookup — fuzzy historical match engine  (Phase 1, Step 1).

This is the first stage of the pipeline and the most straightforward one.
For every new product in the flat file it asks: have we seen something like
this before in the historical FINAL data?  If yes, carry the historical label
across.  If no, find the closest thing and flag it for analyst review.

What it does
------------
  1. Builds a single lookup key per product by concatenating the relevant
     attribute columns — for example, RAW_BRAND + RAW_SUB_BRAND becomes one
     string that represents the product's identity for that attribute.

  2. Does a direct join against the historical data first.  Anything that
     matches exactly gets score=100 and is done.

  3. For products that do not match exactly, runs vectorised rapidfuzz
     token_set_ratio scoring against all known historical keys.  The minimum
     of token_set_ratio and token_sort_ratio is used as the final score so
     that partial string matches like "KRAFT" vs "KRAFT HEINZ" are not
     artificially inflated.  Suggestions below 50 are dropped as noise.

  4. If a product still has no suggestion after fuzzy scoring, and the
     attribute has a recognised catch-all label in its vocabulary (e.g.
     "ALL OTHER BRANDS", "AO COFFEE"), that product is assigned the fallback
     label with score=0.  Score=0 guarantees HIGH QC Priority in Ensemble so
     an analyst always sees it.

  5. Returns one ranked lookup table per attribute plus the flat-file output
     and a flag table that tells Ensemble whether a key maps to one historical
     label or several.

Abbreviation expansion
----------------------
A small set of safe product-name abbreviations are expanded before matching
(CHOC → CHOCOLATE, ORG → ORGANIC etc).  Only unambiguous ones are included
here.  Category-specific synonyms should go in a synonyms.json at project
level, not in this file.

Code style
----------
Functions are written to be read straight through.  Steps are broken into named
variables rather than chained.  If something is not immediately obvious from the
code it has a comment.  Keep it that way.
"""

import re
import sqlite3
import traceback

import numpy as np
import pandas as pd
from rapidfuzz import process as rfprocess, fuzz as rffuzz


# ── Key normalisation ─────────────────────────────────────────────────────────
# Joining attribute tokens with a space (not '') lets token_set_ratio correctly
# tokenise "DARK CHOCOLATE" rather than matching against "DARKCHOCOLATE".
_ABBREV = {
    'CHOC': 'CHOCOLATE',
    'NAT':  'NATURAL',
    'ORG':  'ORGANIC',
    'WHL':  'WHOLE',
    'LF':   'LOW FAT',
    'FF':   'FAT FREE',
    'RF':   'REDUCED FAT',
}
_ABBREV_RE = {re.compile(r'\b' + k + r'\b'): v for k, v in _ABBREV.items()}

# ── "All Other" bucket detection ──────────────────────────────────────────────
# Matches standard catch-all label conventions used across Circana categories:
#   "ALL OTHER"          "ALL OTHERS"          "ALL OTHER BRANDS"
#   "AO BRANDS"          "AO COFFEE"           etc.
# Deliberately excludes:
#   "AO"  alone  — too ambiguous; could be a real brand abbreviation.
#   "OTHER" alone — too generic; matches many non-catch-all labels.
# If no label in the attribute's value space matches, the fallback is skipped
# entirely and the analyst handles the gap — no invented values are written.
_AO_RE = re.compile(r'^(ALL\s+OTHERS?(?:\s|$)|AO\s+\S)', re.IGNORECASE)


def _normalise_key(text: str) -> str:
    """Uppercase, collapse whitespace, expand safe product-name abbreviations."""
    text = str(text).upper().strip()
    text = re.sub(r'\s+', ' ', text)
    for pat, rep in _ABBREV_RE.items():
        text = pat.sub(rep, text)
    return text


def _build_key(row) -> str:
    """Concatenate all values in a DataFrame row into a normalised lookup key."""
    return _normalise_key(' '.join(str(v) for v in row.values))


# ── Internal helpers ──────────────────────────────────────────────────────────

def _score_unmapped_keys(history_df: pd.DataFrame, attr_key_cols: list,
                          mdm_col: str) -> pd.DataFrame:
    """
    Score every unmapped product key against every mapped historical key using
    rapidfuzz token_set_ratio (vectorised).

    Parameters
    ----------
    history_df : DataFrame
        Aggregated historical rows with a 'combinedkey' column and the target
        MDM column.  Rows where the MDM value equals 'missing' are unmapped.
    attr_key_cols : list of str
        Raw key columns for this attribute (carried through to the output).
    mdm_col : str
        Name of the MDM column being matched.

    Returns
    -------
    DataFrame with columns: unmapped, mapped, {mdm_col}, score, attr_key_cols.
    Pre-mapped rows (score=100) are included alongside the scored rows.
    """
    history_df.fillna('missing', inplace=True)

    unmapped  = history_df[history_df[mdm_col] == 'missing'][['combinedkey'] + attr_key_cols].drop_duplicates()
    mapped    = history_df[history_df[mdm_col] != 'missing'][['combinedkey', mdm_col]].drop_duplicates()
    pre_mapped = (
        history_df[history_df[mdm_col] != 'missing'][['combinedkey', mdm_col] + attr_key_cols]
        .drop_duplicates()
        .rename(columns={'combinedkey': 'mapped'})
        .assign(score=100)
    )

    if unmapped.empty:
        mapped['score'] = 100
        mapped['mapped'] = mapped['combinedkey']
        return mapped

    # Vectorised pairwise scoring — replaces cross-join + row-by-row apply.
    unmapped_reset = unmapped.reset_index(drop=True)
    mapped_reset   = mapped.reset_index(drop=True)
    scores_mat = rfprocess.cdist(
        unmapped_reset['combinedkey'].tolist(),
        mapped_reset['combinedkey'].tolist(),
        scorer=rffuzz.token_set_ratio,
        workers=-1,
    )
    unmapped_grid, mapped_grid = np.meshgrid(
        np.arange(len(unmapped_reset)),
        np.arange(len(mapped_reset)),
        indexing='ij',
    )
    unmapped_indices = unmapped_grid.ravel()
    mapped_indices   = mapped_grid.ravel()
    scored = pd.DataFrame({
        'unmapped': unmapped_reset['combinedkey'].values[unmapped_indices],
        'mapped':   mapped_reset['combinedkey'].values[mapped_indices],
        mdm_col:    mapped_reset[mdm_col].values[mapped_indices],
        'score':    scores_mat.ravel(),
    })
    for col in attr_key_cols:
        if col in unmapped_reset.columns:
            scored[col] = unmapped_reset[col].values[unmapped_indices]

    return pd.concat([scored, pre_mapped], ignore_index=True)


def _size_bin_lookup(history_df: pd.DataFrame, results: pd.DataFrame,
                     attr_key_cols: list, mdm_col: str):
    """
    Map raw numeric SIZE values onto labelled size bins via an in-memory SQL
    range join — the most readable and efficient approach for range lookups.

    Returns (results_with_size_col, aggregated_size_sales).
    """
    size_limits = history_df[[mdm_col]].drop_duplicates().dropna()
    size_limits['lower'] = np.where(
        size_limits['SIZE'].str.contains('LESS'), 0,
        size_limits['SIZE'].str.findall(r'(\d+(?:\.\d+)?)').str[0],
    ).astype('float')
    size_limits['higher'] = np.where(
        size_limits['SIZE'].str.contains('PLUS'), 9999,
        size_limits['SIZE'].str.findall(r'(\d+(?:\.\d+)?)').str[1],
    )
    size_limits['higher'] = np.where(
        size_limits['SIZE'].str.contains('LESS'),
        size_limits['SIZE'].str.findall(r'(\d+(?:\.\d+)?)').str[0],
        size_limits['higher'],
    ).astype('float')

    results['combinedkey']        = results['combinedkey'].astype('float')
    history_df[attr_key_cols[0]]  = history_df[attr_key_cols[0]].astype('float')

    conn = sqlite3.connect(':memory:')
    history_df.to_sql('history_df',  conn, index=False)
    size_limits.to_sql('size_limits', conn, index=False)
    results.to_sql('results',        conn, index=False)

    results = pd.read_sql_query(
        "SELECT DISTINCT A.*, B.SIZE FROM results A "
        "LEFT JOIN size_limits B ON A.combinedkey >= B.lower AND A.combinedkey < B.higher",
        conn,
    )
    agg = pd.read_sql_query(
        f"SELECT {attr_key_cols[0]}, B.SIZE, SUM(TOTAL_UNIT_SALES) AS TOTAL_UNIT_SALES "
        f"FROM history_df A LEFT JOIN size_limits B "
        f"ON {attr_key_cols[0]} BETWEEN B.lower AND B.higher "
        f"GROUP BY {attr_key_cols[0]}, B.SIZE",
        conn,
    )
    agg['combinedkey'] = agg[attr_key_cols[0]]
    return results, agg


def _build_attribute_table(flat_file_df: pd.DataFrame, meta_df: pd.DataFrame,
                            history_df: pd.DataFrame):
    """
    Build the base result table by joining flat-file products against historical
    attribute mappings for every MDM attribute group.

    For each attribute:
    - Exact key matches carry the historical MDM value directly (score=100).
    - Unmatched rows are collected for fuzzy scoring in a later step.
    - A 'flag' column marks whether a key combo maps to one (0) or multiple (1)
      distinct historical labels, used by Ensemble to weight confidence.

    Returns
    -------
    tuple of:
        results          – base result DataFrame (one row per flat-file product)
        historical_agg   – aggregated historical records per attribute
        fuzzy_matches    – scored unmatched rows per attribute
        flat_file_combos – unique key combos from the flat file per attribute
        attr_key_map     – {mdm_col: [key_col, ...]}
        flag_map         – {mdm_col: flag DataFrame}
    """
    flat_file_df = flat_file_df.copy()
    flat_file_df['index'] = flat_file_df.index

    # Start results with every column present in the flat file.
    # Each attribute loop reads key columns directly from results — no merging needed.
    results = flat_file_df.copy()

    historical_agg   = {}
    fuzzy_matches    = {}
    flat_file_combos = {}
    attr_key_map     = {}
    flag_map         = {}

    for mdm_col in meta_df['Attribute Group name'].unique():
        attr_key_cols = list(meta_df.loc[meta_df['Attribute Group name'] == mdm_col,
                                         'Attribute Name in MDM'])

        # Key columns are already present in results (flat_file_df.copy()),
        # so no merge is needed here.

        if mdm_col in history_df.columns or mdm_col.replace(' ', '_') in history_df.columns:
            # Build normalised lookup keys from attribute columns
            results['key']            = flat_file_df[attr_key_cols].fillna('').apply(_build_key, axis=1)
            history_df['combinedkey'] = history_df[attr_key_cols].fillna('').apply(_build_key, axis=1)

            # Aggregate historical records: best label per key by sales volume
            historical = history_df.loc[
                :, history_df.columns.isin(
                    attr_key_cols + ['combinedkey', mdm_col, mdm_col.replace(' ', '_'), 'TOTAL_UNIT_SALES']
                )
            ].fillna('')
            agg_cols = [c for c in historical.columns if c != 'TOTAL_UNIT_SALES']
            historical = (
                historical.groupby(agg_cols)
                .agg(TOTAL_UNIT_SALES=('TOTAL_UNIT_SALES', sum), Rec=('TOTAL_UNIT_SALES', 'count'))
                .reset_index()
                .sort_values(agg_cols + ['TOTAL_UNIT_SALES', 'Rec'])
            )
            historical = (
                historical[historical[mdm_col] != '']
                .sort_values(attr_key_cols + ['Rec'], ascending=False)
            )
            historical['rank'] = historical.groupby(attr_key_cols).cumcount(ascending=True) + 1
            historical         = historical[historical['rank'] == 1]

            # Unique key combos from the flat file (for coverage reporting)
            ff_combos    = flat_file_df.loc[
                :, flat_file_df.columns.isin(
                    attr_key_cols + ['UPDATE_REQUIRED', mdm_col, mdm_col.replace(' ', '_')]
                )
            ].fillna('')
            ff_agg_cols  = [c for c in ff_combos.columns if c != 'UPDATE_REQUIRED']
            ff_combos    = ff_combos.groupby(ff_agg_cols).agg(Rec=('UPDATE_REQUIRED', 'count')).reset_index()

            # Resolve actual column name — historical FINAL sheets sometimes use
            # underscores where META uses spaces (e.g. 'PACK TYPE' vs 'PACK_TYPE').
            # Taking the first match normalises to whatever variant exists in history.
            mdm_col = list(history_df.columns[
                history_df.columns.isin([mdm_col, mdm_col.replace(' ', '_')])
            ])[0]
            attr_key_map[mdm_col] = attr_key_cols

            # Direct join: flat-file keys → best historical mapping by sales
            best_mapping = (
                historical[['combinedkey', mdm_col, 'TOTAL_UNIT_SALES', 'Rec']]
                .groupby(['combinedkey', mdm_col])
                .agg(TOTAL_UNIT_SALES=('TOTAL_UNIT_SALES', sum), Rec=('Rec', sum))
                .reset_index()
                .sort_values('TOTAL_UNIT_SALES', ascending=False)
            )
            best_mapping['rank'] = (
                best_mapping.groupby(['combinedkey'])['TOTAL_UNIT_SALES']
                .rank(method='dense', ascending=False)
            )
            results = pd.merge(
                results,
                best_mapping.loc[best_mapping['rank'] == 1, ['combinedkey', mdm_col]],
                left_on='key', right_on='combinedkey', how='left', suffixes=('', '_remove'),
            )

            # Flag: does this key combo map to one label (0) or multiple (1)?
            flag_df          = history_df[attr_key_cols + [mdm_col]].drop_duplicates()
            flag_df['count'] = flag_df.groupby(attr_key_cols)[mdm_col].transform('count')
            flag_df['flag']  = (flag_df['count'] > 1).astype(int)
            flag_map[mdm_col] = flag_df[attr_key_cols + ['flag']].drop_duplicates()

            results = results[[c for c in results.columns if 'key' not in c and '_remove' not in c]]

        elif mdm_col == 'SIZE':
            results['combinedkey'] = flat_file_df[attr_key_cols].fillna('').apply(_build_key, axis=1)
            results, historical    = _size_bin_lookup(history_df, results, attr_key_cols, mdm_col)
            historical['UNIT_SHARE'] = historical['TOTAL_UNIT_SALES'] / historical['TOTAL_UNIT_SALES'].sum()
            ff_combos              = historical
            attr_key_map[mdm_col]  = attr_key_cols

        else:
            history_df['combinedkey'] = history_df[attr_key_cols]
            results[mdm_col]          = history_df[attr_key_cols]
            historical = history_df.loc[
                :, history_df.columns.isin(
                    attr_key_cols + ['combinedkey', mdm_col, mdm_col.replace(' ', '_'), 'TOTAL_UNIT_SALES']
                )
            ].fillna('')
            agg_cols   = [c for c in historical.columns if c != 'TOTAL_UNIT_SALES']
            historical = (
                historical.groupby(agg_cols)
                .agg(TOTAL_UNIT_SALES=('TOTAL_UNIT_SALES', sum), Rec=('TOTAL_UNIT_SALES', 'count'))
                .reset_index()
                .sort_values(agg_cols + ['TOTAL_UNIT_SALES', 'Rec'])
            )
            historical['rank'] = historical.groupby(agg_cols).cumcount(ascending=False) + 1
            historical         = historical[historical['rank'] == 1]
            ff_combos          = flat_file_df.loc[
                :, flat_file_df.columns.isin(
                    attr_key_cols + ['UPDATE_REQUIRED', mdm_col, mdm_col.replace(' ', '_')]
                )
            ].fillna('missing')
            ff_agg_cols        = [c for c in ff_combos.columns if c != 'UPDATE_REQUIRED']
            ff_combos          = ff_combos.groupby(ff_agg_cols).agg(Rec=('UPDATE_REQUIRED', 'count')).reset_index()
            attr_key_map[mdm_col] = attr_key_cols

        historical_agg[mdm_col]   = historical
        flat_file_combos[mdm_col] = ff_combos
        fuzzy_matches[mdm_col]    = _score_unmapped_keys(historical, attr_key_cols, mdm_col)
        print(f"  Lookup   {mdm_col}: {len(ff_combos)} products to map | {len(historical)} matched to history")

    return results.drop(columns=['index']), historical_agg, fuzzy_matches, flat_file_combos, attr_key_map, flag_map


def _select_top_matches(fuzzy_matches: dict) -> dict:
    """
    Retain only the top-ranked fuzzy match per unmapped key.

    For SIZE the threshold is score=100 (exact bin match required).
    For all other attributes the top-scoring candidate per key is kept
    regardless of score — low-confidence suggestions are filtered in
    _build_lookup_table (threshold ≥50).
    """
    for mdm_col, match_df in fuzzy_matches.items():
        try:
            if 'unmapped' not in match_df.columns:
                # All items matched directly — no fuzzy ranking needed.
                continue
            match_df['rank'] = match_df.groupby('unmapped')['score'].rank(
                method='dense', ascending=False
            )
            if mdm_col == 'SIZE':
                # SIZE requires an exact bin match — partial matches are meaningless
                fuzzy_matches[mdm_col] = match_df[match_df['score'] == 100]
            else:
                fuzzy_matches[mdm_col] = match_df[match_df['rank'] == 1]
        except Exception as exc:
            print(f"  WARNING: fuzzy rank failed for '{mdm_col}': {exc} — no lookup suggestions")
    return fuzzy_matches


def _build_flat_file_output(flat_file_df: pd.DataFrame, meta_df: pd.DataFrame):
    """
    Build the FLAT_FILE output sheet from the raw flat-file data.

    Adds IS_NEW_UPC and RAW_IS_ACTIVE sentinel columns, and appends the
    MDM attribute columns so the sheet carries the full template structure.
    Returns (output_df, column_list).
    """
    output = flat_file_df.copy()
    output['IS_NEW_UPC']    = 0
    output['RAW_IS_ACTIVE'] = 1
    flat_file_columns = list(output.columns) + list(meta_df['Attribute Name in MDM'])
    return output, flat_file_columns


def _build_lookup_table(mapped_df: pd.DataFrame, new_products_df: pd.DataFrame,
                         attr_key_cols: list, mdm_col: str):
    """
    Build the ranked lookup table for a single attribute.

    Combines:
    - Directly matched products (score=100 from historical join).
    - Fuzzy-matched new products (vectorised min of token_set_ratio and
      token_sort_ratio; threshold ≥50 to suppress noise).

    The min-of-two-scorers approach prevents subset-match inflation —
    e.g. "KRAFT" vs "KRAFT HEINZ" scores 100 on token_set but 72 on
    token_sort, so the combined score is 72, which is more accurate.

    Returns (matched_df, lookup_table_df).
    lookup_table_df columns: attr_key_cols + [mdm_col, score, Rank, Record].
    'missing' values are replaced with blank before returning.
    """
    new_products_df = new_products_df.copy()
    new_products_df['mapped'] = new_products_df[attr_key_cols].fillna('').apply(_build_key, axis=1)
    directly_matched = pd.merge(new_products_df, mapped_df[['mapped', mdm_col, 'score']], how='inner')
    remaining        = new_products_df[~new_products_df['mapped'].isin(directly_matched['mapped'])]

    if remaining.empty:
        lookup_table = directly_matched[attr_key_cols + [mdm_col]].fillna(100).drop_duplicates()
        lookup_table['score'] = 100
        lookup_table[mdm_col] = lookup_table[mdm_col].astype(str)
    else:
        remaining_r = remaining.drop(columns=mdm_col, errors='ignore').reset_index(drop=True)
        mapped_lkp  = mapped_df[['mapped', mdm_col]].drop_duplicates('mapped').reset_index(drop=True)

        u_keys = remaining_r['mapped'].tolist()
        m_keys = mapped_lkp['mapped'].tolist()

        mat_set    = rfprocess.cdist(u_keys, m_keys, scorer=rffuzz.token_set_ratio,  workers=-1)
        mat_sort   = rfprocess.cdist(u_keys, m_keys, scorer=rffuzz.token_sort_ratio, workers=-1)
        scores_mat = np.minimum(mat_set, mat_sort)

        # Suppress numeric↔non-numeric cross-pairings (e.g. "16OZ" vs "CHEDDAR").
        # Broadcasting [N,1] != [1,M] produces an [N,M] boolean mask — True where
        # the unmapped key has digits and the mapped key does not (or vice versa).
        u_has_digits = np.array([bool(re.search(r'\d+', str(k))) for k in u_keys])
        m_has_digits = np.array([bool(re.search(r'\d+', str(k))) for k in m_keys])
        numeric_type_mismatch = u_has_digits[:, None] != m_has_digits[None, :]
        scores_mat[numeric_type_mismatch] = 0

        # Exact string match always → 100 (overrides scorer floating-point rounding).
        # Same [N,M] broadcasting: True wherever the unmapped key equals the mapped key.
        u_arr, m_arr = np.array(u_keys), np.array(m_keys)
        exact_match_mask = u_arr[:, None] == m_arr[None, :]
        scores_mat[exact_match_mask] = 100

        best_idx    = np.argmax(scores_mat, axis=1)
        best_scores = scores_mat[np.arange(len(u_keys)), best_idx]

        fuzzy_matched                = remaining_r.copy()
        fuzzy_matched['newmapping']  = fuzzy_matched['mapped']
        fuzzy_matched['mapped']      = mapped_lkp['mapped'].iloc[best_idx].values
        fuzzy_matched[mdm_col]       = mapped_lkp[mdm_col].iloc[best_idx].values
        fuzzy_matched['score']       = best_scores

        # Below 50 is noise — suppress to avoid polluting the analyst view.
        fuzzy_matched = fuzzy_matched[fuzzy_matched['score'] >= 50]

        # ── "All Other" fallback ──────────────────────────────────────────
        # If a standard catch-all bucket exists in the label space, any flat-
        # file key that still has no match (score < 50 for every candidate) is
        # assigned it with score=0.  This ensures every product appears in the
        # lookup table so Phase 2 can fill the cell rather than leaving it blank.
        # Score=0 guarantees QC Priority=HIGH in Ensemble so the analyst sees it.
        # Deduplicate to unique AO-style labels only — the same label will
        # appear once per historical key that uses it (e.g. 646× "AO BRAND"),
        # so iterating the raw column would give a list of length 646, not 1,
        # causing the single-AO-bucket guard to incorrectly suppress the fallback.
        ao_label_candidates = list({
            lbl for lbl in mapped_lkp[mdm_col].dropna().str.strip()
            if _AO_RE.match(lbl)
        })
        # Only apply the fallback when exactly one AO bucket exists.
        # Multiple distinct AO labels (e.g. "AO MAINSTREAM BRANDS" + "AO VALUE BRANDS")
        # are ambiguous — assigning the wrong bucket is worse than leaving
        # the cell blank for the analyst to handle.
        ao_label = ao_label_candidates[0] if len(ao_label_candidates) == 1 else None
        if ao_label is not None:
            already_matched_keys = set(fuzzy_matched['newmapping'].tolist()) if not fuzzy_matched.empty else set()
            fallback_rows        = remaining_r[~remaining_r['mapped'].isin(already_matched_keys)].copy()
            if not fallback_rows.empty:
                fallback_rows[mdm_col] = ao_label
                fallback_rows['score'] = 0
                print(f"    AO fallback: {len(fallback_rows)} unmatched key(s) → '{ao_label}'")
                fuzzy_matched = pd.concat([fuzzy_matched, fallback_rows], ignore_index=True)

        if not fuzzy_matched.empty:
            lookup_table = pd.concat([
                directly_matched[attr_key_cols + [mdm_col, 'score']],
                fuzzy_matched[attr_key_cols + [mdm_col, 'score']],
            ]).fillna(100).drop_duplicates()
        else:
            lookup_table = directly_matched[attr_key_cols + [mdm_col, 'score']]

        lookup_table[mdm_col]  = lookup_table[mdm_col].astype(str)
        directly_matched       = pd.concat([directly_matched, fuzzy_matched]).drop_duplicates()

    # ── Full-coverage guarantee ───────────────────────────────────────────────
    # Every key combo present in the flat file must have at least one lkp row,
    # even if the algorithm couldn't assign a value.  Without this, Phase 1
    # silently drops unresolvable key combos and Phase 2's left-join returns
    # NaN with no analyst visibility.  Blank-value rows (score=0) make the gap
    # explicit: the analyst sees them in the lkp, fills them in, and Phase 2
    # then picks up the corrected value — satisfying the design contract that
    # all MODELING attributes are filled before the pipeline continues.
    _ff_keys  = new_products_df[attr_key_cols].drop_duplicates()
    _lkp_keys = lookup_table[attr_key_cols].drop_duplicates()
    _missing  = pd.merge(_ff_keys, _lkp_keys, on=attr_key_cols, how='left', indicator=True)
    _missing  = _missing[_missing['_merge'] == 'left_only'].drop(columns=['_merge'])
    if not _missing.empty:
        _missing = _missing.copy()
        _missing[mdm_col] = ''
        _missing['score'] = 0
        print(f"    Coverage gap: {len(_missing)} key combo(s) have no historical match — "
              f"written as blank for analyst assignment; review input key column values for these products")
        lookup_table = pd.concat(
            [lookup_table, _missing[attr_key_cols + [mdm_col, 'score']]],
            ignore_index=True,
        )

    lookup_table['flag'] = 1
    lookup_table['Rank'] = (
        lookup_table[attr_key_cols + ['flag']]
        .groupby(attr_key_cols)['flag']
        .rank(method='dense', ascending=False)
    )
    lookup_table.drop(columns=['flag'], inplace=True)
    lookup_table = pd.merge(
        lookup_table,
        lookup_table[attr_key_cols + [mdm_col]]
            .groupby(attr_key_cols).agg(Record=(mdm_col, 'count')).reset_index(),
        on=attr_key_cols,
    )
    lookup_table.replace({'missing': ''}, regex=True, inplace=True)
    return directly_matched, lookup_table


# ── Public entry point ────────────────────────────────────────────────────────

def runLookup(flat_file_df: pd.DataFrame, meta_df: pd.DataFrame,
              history_df: pd.DataFrame, recom_dict: dict) -> tuple:
    """
    Run the full Lookup stage for all MODELING attributes defined in META.

    Parameters
    ----------
    flat_file_df : DataFrame
        New products to classify (the flat-file CSV, string-coerced).
    meta_df : DataFrame
        META sheet — defines attribute groups and their key columns.
    history_df : DataFrame
        Historical FINAL data — the labelled training reference.
    recom_dict : dict
        Accumulator for all predictor outputs.  Lookup results are added
        as "Lookup_{attrG}" keys.

    Returns
    -------
    (recom_dict, history_df, flat_file_output, flag_map)
    """
    base_results, historical_agg, fuzzy_matches, flat_file_combos, attr_key_map, flag_map = (
        _build_attribute_table(flat_file_df, meta_df, history_df)
    )
    top_matches      = _select_top_matches(fuzzy_matches)
    flat_file_output, _ = _build_flat_file_output(flat_file_df, meta_df)

    for mdm_col in historical_agg:
        try:
            _, recom_dict[f'Lookup_{mdm_col}'] = _build_lookup_table(
                top_matches[mdm_col], flat_file_combos[mdm_col], attr_key_map[mdm_col], mdm_col
            )
        except Exception as exc:
            print(f"  ERROR: Lookup failed for '{mdm_col}': {exc}")
            print(traceback.format_exc())

    return recom_dict, history_df, flat_file_output, flag_map
