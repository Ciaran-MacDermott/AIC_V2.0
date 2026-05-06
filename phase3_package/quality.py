"""
Phase 3 quality checks and validation functions.

Each function in this module corresponds to a numbered pipeline step.
Functions either modify the DataFrame (returning the updated copy) or
perform read-only QC checks that print results to the log.

Step Index (matching pipeline.py)
---------------------------------
 1  update_req_check              – Ensure UPDATE_REQUIRED = 1 for all rows.
 2  verify_ao_cat_def             – Align ASSORTMENT_CATEGORY_DEFINITION to ModelInfo.
 3  demand_group_check            – Audit DEMAND_GROUP and interaction columns.
 6  check_identifier_numeric_format – Fix scientific notation / decimals in identifiers.
 8  flag_invalid_headers          – Drop unnamed columns, flag non-standard names.
 9  check_special_chars           – Replace '/' with ' OR ', flag special characters.
10  check_duplicate_dimkeys       – Deduplicate ITEM_DIM_KEYs (keep highest dollar).
13  check_brand_tool_brand_mismatch – Flag unexpected BRAND vs TOOL_BRAND differences.
14  check_null_modeling_reporting_cols – Report nulls in modeling/reporting columns.
    split_by_raw_assortment_category  – Split output by RAW_ASSORTMENT_CATEGORY (Post-QC).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from pandas.api.types import (
    is_string_dtype,
    is_object_dtype,
    is_integer_dtype,
    is_float_dtype,
)


# ═══════════════════════════════════════════════════════════════════════════
# Formatting Constants & Helpers
# ═══════════════════════════════════════════════════════════════════════════
MAJOR_SEP = "=" * 70
MINOR_SEP = "-" * 60
INDENT = "   "


def _indent_block(text: str, indent: str = INDENT) -> str:
    """Indent every line of *text* with the given prefix."""
    return indent + str(text).replace("\n", "\n" + indent)


def _print_step_header(step: str, title: str) -> None:
    """Print a consistently formatted step header (e.g. ``3) Demand Group Check``).

    The leading double newline puts a blank line between segments so each
    step stands out against its predecessor's output, rather than abutting
    it.
    """
    print(f"\n\n{step}) {title}")
    print(MINOR_SEP)


def _print_df(
    df: pd.DataFrame,
    *,
    title: str | None = None,
    indent: str = INDENT,
    max_rows: int | None = None,
    min_col_width: int = 12,
) -> None:
    """Pretty-print a DataFrame for log output (no index, consistent indentation)."""
    if title:
        print(_indent_block(title, indent=indent))
    if df is None:
        print(_indent_block("(None)", indent=indent))
        return
    if df.empty:
        print(_indent_block("(no rows)", indent=indent))
        return

    view = df.head(max_rows) if max_rows is not None else df
    with pd.option_context("display.max_colwidth", None, "display.width", None):
        formatted = view.to_string(index=False, col_space=min_col_width)
    print(_indent_block(formatted, indent=indent))


def _is_text_dtype(series: pd.Series) -> bool:
    """Return True if *series* is string-like (object, string, or categorical)."""
    return isinstance(series.dtype, pd.CategoricalDtype) or is_string_dtype(series) or is_object_dtype(series)


# ═══════════════════════════════════════════════════════════════════════════
# Step 1: UPDATE_REQUIRED Check
# ═══════════════════════════════════════════════════════════════════════════

def update_req_check(df: pd.DataFrame, col: str = "UPDATE_REQUIRED") -> pd.DataFrame:
    """Set UPDATE_REQUIRED to 1 for any rows currently set to 0."""
    col_upper_map = {str(c).upper(): c for c in df.columns}
    col = col_upper_map.get(col.upper(), col)

    df[col] = pd.to_numeric(df[col], errors="coerce")
    zero_mask = df[col].eq(0)

    _print_step_header("1", "UPDATE_REQUIRED Check")
    if zero_mask.any():
        rows_changed = int(zero_mask.sum())
        df.loc[zero_mask, col] = 1
        print(f"{INDENT}✓ Converted {rows_changed} zero values to 1 in {col}.")
    else:
        print(f"{INDENT}✓ No zero values found in {col}.")
    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 2: Assortment Category Definition Check
# ═══════════════════════════════════════════════════════════════════════════

def verify_ao_cat_def(
    df: pd.DataFrame,
    model_info: pd.DataFrame,
    col: str = "ASSORTMENT_CATEGORY_DEFINITION",
) -> pd.DataFrame:
    """Align ASSORTMENT_CATEGORY_DEFINITION values to the ModelInfo Category_Name."""
    col_upper_map = {str(c).upper(): c for c in df.columns}
    col = col_upper_map.get(col.upper(), col)

    model_col_upper_map = {str(c).upper(): c for c in model_info.columns}
    category_name_col = model_col_upper_map.get("CATEGORY_NAME", "Category_Name")

    expected_category = model_info[category_name_col].unique()[0]
    current_values = df[col].unique()

    _print_step_header("2", "Assortment Category Definition Check")
    print(f"{INDENT}ModelInfo Category:    {expected_category}")
    print(f"{INDENT}Current values in {col}: {list(current_values)}")

    # Replace any value that doesn't match the expected category
    for value in current_values:
        if value != expected_category:
            df.loc[df[col] == value, col] = expected_category

    print(f"{INDENT}After alignment cleaning :       {list(df[col].unique())}")
    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 3: Demand Group Check
# ═══════════════════════════════════════════════════════════════════════════

def demand_group_check(
    df: pd.DataFrame,
    demand_group_fallback: str = None,
) -> pd.DataFrame:
    """
    Report blanks in DEMAND_GROUP and audit any interaction columns.

    If DEMAND_GROUP has blanks and only one distinct non-blank value,
    the blanks are auto-filled. If ALL values are blank but a fallback
    value is available from the FINAL template tab, that value is used
    with a warning. Otherwise the user is advised to check input data.
    """
    col_upper_map = {str(c).upper(): c for c in df.columns}
    demand_group_col = col_upper_map.get("DEMAND_GROUP")
    has_demand_group = demand_group_col is not None

    _print_step_header("3", "Demand Group Check")
    print(f"{INDENT}DEMAND_GROUP column present: {has_demand_group}")

    if has_demand_group:
        demand_group_series = df[demand_group_col]
        blank_mask = (
            demand_group_series.isna()
            | demand_group_series.astype(str).str.strip().eq("")
        )
        print(f"{INDENT}Has blank values:            {blank_mask.any()}")

        if blank_mask.any():
            blank_count = int(blank_mask.sum())
            print(f"{INDENT}Blank count:                 {blank_count} of {len(demand_group_series)}")

            non_blank_values = (
                demand_group_series[~blank_mask]
                .dropna()
                .astype(str)
                .str.strip()
                .unique()
            )
            print(f"{INDENT}Unique values (non-blank):   {list(non_blank_values)}")

            # Safe to auto-fill only when there's exactly one distinct value
            if len(non_blank_values) == 1:
                fill_value = non_blank_values[0]
                df.loc[blank_mask, demand_group_col] = fill_value
                print(f"{INDENT}✓ Replaced {blank_count} blank values with '{fill_value}'")
            elif len(non_blank_values) == 0:
                # All values blank — try FINAL template fallback
                if demand_group_fallback:
                    df.loc[blank_mask, demand_group_col] = demand_group_fallback
                    print(f"{INDENT}⚠ FLAT_FILE DEMAND_GROUP is entirely blank ({blank_count} rows).")
                    print(f"{INDENT}  Populated from FINAL template tab with: '{demand_group_fallback}'")
                    print(f"{INDENT}  Please verify this value matches the project scope form.")
                else:
                    print(f"{INDENT}⚠ FLAT_FILE DEMAND_GROUP is entirely blank ({blank_count} rows).")
                    print(f"{INDENT}  No value found in FINAL template tab either.")
                    print(f"{INDENT}  Check the project scope form and populate DEMAND_GROUP manually.")
            else:
                print(f"{INDENT}⚠ Multiple non-blank values found — blanks not replaced.")

    # Audit interaction columns (if any exist)
    interaction_columns = [
        col_name for col_name in df.columns
        if "interaction" in str(col_name).lower()
    ]

    if not interaction_columns:
        print(f"\n{INDENT}No interaction columns found.")
        return df

    print(f"{INDENT}Interaction columns ({len(interaction_columns)}):")
    for col_name in interaction_columns:
        series = df[col_name]
        if _is_text_dtype(series):
            as_string = series.astype("string")
            blank_mask = as_string.isna() | as_string.str.strip().eq("")
        else:
            blank_mask = series.isna()

        status = f"blanks: {int(blank_mask.sum())}" if blank_mask.any() else "✓ no blanks"
        print(f"{INDENT}  • {col_name}: {status}")

    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 6: Identifier Numeric Format Check
# ═══════════════════════════════════════════════════════════════════════════

def check_identifier_numeric_format(
    df: pd.DataFrame,
    cols: tuple = ("UPC10", "SKU", "ITEM_DIM_KEY"),
    show_examples: bool = True,
) -> None:
    """
    Audit identifier columns for scientific notation and decimal formatting.

    Auto-fixes whole-number values stored as strings or floats by converting
    them to Int64. Leaves genuinely fractional values untouched with a warning.
    """
    _print_step_header("6", "UPC Column Format Check (scientific notation / decimals)")

    col_upper_map = {str(c).upper(): c for c in df.columns}

    # Resolve requested columns to their actual DataFrame names
    resolved_columns: List[str] = []
    missing_columns: List[str] = []
    for col_name in cols:
        if col_name.upper() in col_upper_map:
            resolved_columns.append(col_upper_map[col_name.upper()])
        else:
            missing_columns.append(col_name)

    if missing_columns:
        print(f"{INDENT}Missing columns (skipped): {missing_columns}")
    if not resolved_columns:
        print(f"{INDENT}No valid columns to check. Exiting.")
        return

    print(f"{INDENT}Ensuring identifier values are in correct data format")

    for col_name in resolved_columns:
        series = df[col_name]
        print(f"\n{INDENT}{col_name} (dtype: {series.dtype})")

        # Already integer — nothing to fix
        if is_integer_dtype(series):
            print(f"{INDENT}  ✓ Already integer type")
            continue

        series_as_string = series.astype("string")
        series_as_numeric = pd.to_numeric(series, errors="coerce")

        # Detection masks
        scientific_notation_mask = series_as_string.str.match(
            r"^[\+\-]?\d+(?:\.\d+)?[eE][\+\-]?\d+$", na=False
        )
        decimal_string_mask = series_as_string.str.contains(r"\.\d+", na=False)
        non_integer_mask = series_as_numeric.notna() & (np.floor(series_as_numeric) != series_as_numeric)

        # Report findings based on dtype
        if _is_text_dtype(series):
            print(f"{INDENT}  • Strings in scientific notation: {int(scientific_notation_mask.sum())}")
            print(f"{INDENT}  • Strings with decimal places:    {int(decimal_string_mask.sum())}")
            print(f"{INDENT}  • Numeric non-integers:           {int(non_integer_mask.sum())}")
        elif is_float_dtype(series):
            not_null = series.notna()
            fractional_mask = not_null & ~np.isclose(series, np.round(series), rtol=0, atol=1e-12)
            print(f"{INDENT}  • Float non-integers (fractional): {int(fractional_mask.sum())}")
        else:
            print(f"{INDENT}  • Column dtype not string/float/integer; skipped.")

        # Auto-fix: convert whole-number values to Int64
        is_whole_number = series_as_numeric.notna() & np.isclose(
            series_as_numeric, np.round(series_as_numeric), rtol=0, atol=1e-12
        )

        if _is_text_dtype(series):
            if is_whole_number.any():
                df.loc[is_whole_number, col_name] = (
                    pd.to_numeric(series_as_string[is_whole_number], errors="coerce")
                    .round()
                    .astype("Int64")
                )
                print(f"{INDENT}  ✓ Auto-fix: converted {int(is_whole_number.sum())} string values to Int64.")

        elif is_float_dtype(series):
            not_null = series.notna()
            float_is_whole = not_null & np.isclose(series, np.round(series), rtol=0, atol=1e-12)

            if not_null.any():
                if (float_is_whole | ~not_null).all():
                    df[col_name] = pd.Series(np.round(series), index=series.index).astype("Int64")
                    print(f"{INDENT}  ✓ Auto-fix: entire float column converted to Int64 (all values are whole).")
                else:
                    fractional_count = int((not_null & ~float_is_whole).sum())
                    print(f"{INDENT}  • Auto-fix skipped: {fractional_count} non-integer values present.")


# ═══════════════════════════════════════════════════════════════════════════
# Step 8: Column Name Validation
# ═══════════════════════════════════════════════════════════════════════════

def flag_invalid_headers(
    df: pd.DataFrame,
    allowed_pattern: str = r"^[A-Z0-9_]+$",
) -> pd.DataFrame:
    """
    Drop auto-generated ``Unnamed: N`` columns and flag any remaining
    column names that don't match the A-Z / 0-9 / underscore convention.
    """
    column_names = pd.Index(map(str, df.columns))
    invalid_columns = column_names[~column_names.str.fullmatch(allowed_pattern)].tolist()

    unnamed_pattern = re.compile(r"^Unnamed:\s*\d+$", re.IGNORECASE)
    unnamed_columns = [col for col in invalid_columns if unnamed_pattern.match(col)]
    if unnamed_columns:
        df = df.drop(columns=unnamed_columns)

    other_invalid = [col for col in invalid_columns if col not in unnamed_columns]

    _print_step_header("8", "Column Name Validation")
    if other_invalid:
        print(f"{INDENT}⚠ Invalid column names: {other_invalid}")
    else:
        print(f"{INDENT}✓ All column names are valid")
    if unnamed_columns:
        print(f"{INDENT}✓ Dropped unnamed columns: {unnamed_columns}")

    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 9: Special Character Check
# ═══════════════════════════════════════════════════════════════════════════

def check_special_chars(
    df: pd.DataFrame,
    suffix: Optional[str] = "rptg",
    exclude_prefix: Optional[str] = "raw",
) -> pd.DataFrame:
    """
    Replace ``/`` with ``' OR '`` in selected columns and flag any columns
    that contain special characters (em-dash, en-dash, ampersand, etc.).

    DESCRIPTION columns are always excluded from replacement.
    """
    characters_to_flag = "—–&/<>="
    char_regex_class = "[" + re.escape(characters_to_flag) + "]"

    def _should_check_column(col_name: str) -> bool:
        """Return True if the column matches the suffix/prefix filter."""
        lower_name = str(col_name).lower()
        if lower_name == "description":
            return False
        has_suffix = True if suffix is None else lower_name.endswith(str(suffix).lower())
        is_excluded = False if exclude_prefix is None else lower_name.startswith(str(exclude_prefix).lower())
        return has_suffix and not is_excluded

    target_columns = [col for col in df.columns if _should_check_column(col)]

    _print_step_header("9", "Special Character Check")

    if not target_columns:
        scope_parts = []
        if suffix is not None:
            scope_parts.append(f"suffix '{suffix}'")
        if exclude_prefix is not None:
            scope_parts.append(f"not starting with prefix '{exclude_prefix}'")
        scope_description = " & ".join(scope_parts) if scope_parts else "all columns"
        print(f"{INDENT}No columns matched the selection ({scope_description}).")
        return df

    # --- Flag columns containing special characters ------------------------
    columns_with_specials: Dict[str, List[str]] = {}
    for col_name in target_columns:
        found_chars = df[col_name].astype(str).str.findall(char_regex_class).explode().dropna()
        if not found_chars.empty:
            unique_chars = sorted(set(found_chars.tolist()))
            if unique_chars:
                columns_with_specials[str(col_name)] = unique_chars

    if not columns_with_specials:
        print(f"{INDENT}✓ No special characters found.")
    else:
        print(f"{INDENT}Columns with special characters:")
        for col_name, chars in columns_with_specials.items():
            print(f"{INDENT}  • {col_name}: {' '.join(chars)}")

    # --- Apply replacements (e.g. '/' → ' OR ') ---------------------------
    # Handle all spacing combos (A/B, A / B, A/ B, A /B) by replacing any
    # optional surrounding whitespace + slash with ' OR ', avoiding double spaces.
    columns_replaced: List[str] = []
    for col_name in target_columns:
        original_values = df[col_name].astype(str)
        updated_values = original_values

        if updated_values.str.contains("/", regex=False).any():
            updated_values = updated_values.str.replace(
                r" ?/ ?", " OR ", regex=True
            )
            if not original_values.equals(updated_values):
                df[col_name] = updated_values
                columns_replaced.append(col_name)

    if columns_replaced:
        print(f"\n{INDENT}Replacements applied ('/' → ' OR '):")
        for col_name in columns_replaced:
            print(f"{INDENT}  ✓ {col_name}")

    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 10: Duplicate DimKey Check
# ═══════════════════════════════════════════════════════════════════════════

def check_duplicate_dimkeys(
    df: pd.DataFrame,
    col: str = "ITEM_DIM_KEY",
    dollars_col: str = "RAW_TOTAL_DOLLARS",
    ignore_nulls: bool = True,
    show_examples: bool = False,
    max_examples: int = 5,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Find duplicate ITEM_DIM_KEYs and deduplicate by keeping the highest-dollar row.

    Returns
    -------
    (deduplicated_df, duplicate_summary_df)
        duplicate_summary_df has columns [col, "Count"] listing which keys
        had duplicates and how many.
    """
    _print_step_header("10", "Duplicate DimKey Check")
    print(
        f"{INDENT}Note: An early duplicate dim key check also runs in Phase 2 "
        f"(before attribute processing) to prevent row explosion during ranking."
    )
    print(f"{INDENT}This is a safety-net check — no duplicates are expected at this stage.")

    col_upper_map = {str(c).upper(): c for c in df.columns}

    # Resolve column names (case-insensitive)
    if col.upper() not in col_upper_map:
        print(f"{INDENT}Column '{col}' not found.")
        empty_summary = pd.DataFrame(columns=[col, "Count"])
        return df, empty_summary
    col = col_upper_map[col.upper()]

    dollars_col_resolved = col_upper_map.get(dollars_col.upper())

    # Build masks for null/blank keys
    key_series = df[col]
    key_as_string = key_series.astype("string")
    is_null = key_series.isna()
    is_blank = key_as_string.str.strip().eq("")

    if ignore_nulls:
        valid_mask = ~(is_null | is_blank)
        keys_to_check = key_series[valid_mask]
    else:
        keys_to_check = key_series

    # Count occurrences and identify duplicates
    value_counts = keys_to_check.value_counts(dropna=False)
    duplicate_counts = value_counts[value_counts >= 2]

    if duplicate_counts.empty:
        print(f"{INDENT}✓ No duplicate values found in '{col}'.")
        empty_summary = pd.DataFrame(columns=[col, "Count"])
        return df, empty_summary

    # Build QC summary of duplicate keys
    duplicate_summary = (
        duplicate_counts.rename("Count")
        .reset_index()
        .rename(columns={"index": col})
        .sort_values(["Count", col], ascending=[False, True], ignore_index=True)
    )

    # If no dollars column, report but don't drop
    if not dollars_col_resolved:
        print(
            f"{INDENT}Found {len(duplicate_summary)} duplicate {col} values; "
            f"no '{dollars_col}' column present—no rows were dropped."
        )
        if show_examples:
            example_rows = df[df[col].isin(duplicate_summary[col])].sort_values([col]).head(max_examples)
            print(f"{INDENT}Example duplicate rows (no dollars col):")
            _print_df(example_rows[[col]], indent=INDENT)

        return df, duplicate_summary

    # Deduplicate: keep the row with the highest dollar value per key
    dollar_values = pd.to_numeric(df[dollars_col_resolved], errors="coerce")
    dollar_values_filled = dollar_values.fillna(float("-inf"))

    duplicate_key_set = set(duplicate_summary[col])
    is_duplicate_row = df[col].isin(duplicate_key_set)
    if ignore_nulls:
        is_duplicate_row &= ~(is_null | is_blank)

    # Index of the highest-dollar row for each duplicate key
    rows_to_keep = (
        dollar_values_filled[is_duplicate_row]
        .groupby(df.loc[is_duplicate_row, col], sort=False)
        .idxmax()
    )

    rows_to_drop = is_duplicate_row & ~df.index.isin(rows_to_keep.values)
    dropped_count = int(rows_to_drop.sum())
    deduplicated_df = df.loc[~rows_to_drop].copy()

    print(f"{INDENT}Found {len(duplicate_summary)} duplicate {col} values; dropped {dropped_count} row(s)")
    print(f"{INDENT}Keeping highest '{dollars_col_resolved}' per {col}.")

    if show_examples and dropped_count:
        display_columns = [col, dollars_col_resolved]
        kept_examples = (
            df.loc[rows_to_keep.values, display_columns]
            .sort_values([col, dollars_col_resolved], ascending=[True, False])
            .head(max_examples)
        )
        print(f"{INDENT}Kept rows (sorted by key, dollars desc):")
        _print_df(kept_examples, indent=INDENT)

    return deduplicated_df, duplicate_summary


# ═══════════════════════════════════════════════════════════════════════════
# Step 13: BRAND vs TOOL_BRAND Mismatch Check
# ═══════════════════════════════════════════════════════════════════════════

def check_brand_tool_brand_mismatch(
    df: pd.DataFrame,
    brand_col: str = "BRAND",
    tool_brand_col: str = "TOOL_BRAND",
    raw_manufacturer_col: str = "",
    raw_parent_col: str = "",
    valid_model_suffixes: set = None,
) -> List[Dict[str, Any]]:
    """
    Compare BRAND vs TOOL_BRAND (including model-suffixed variants) and
    report unexpected mismatches.

    When *raw_parent_col* (preferred) or *raw_manufacturer_col* (legacy
    fallback) resolves to a real column and any mismatched BRAND or
    TOOL_BRAND value starts with ``"AO "``, the parent column is included
    in the distinct-pair grouping so that each parent gets its own row in
    the resolution dialog — and surfaced as the dialog's ``PARENT``
    header.  Splitting parent and manufacturer means analysts can see
    retailer values (e.g. "CVS PHARMACY") in the dialog without losing
    manufacturer-side cleanup configurability.

    Returns a list of per-model mismatch groups.  Each group is a dict::

        {
            "model_suffix": str,        # "" for base, "MULO", "CONV", etc.
            "brand_col": str,           # actual column name in df
            "tool_brand_col": str,      # actual column name in df
            "mismatch_df": DataFrame,   # columns: BRAND, TOOL_BRAND[, PARENT]
        }

    Only groups with at least one mismatch are included.
    Returns an empty list when there are no mismatches.
    """
    _print_step_header("13", "BRAND vs TOOL_BRAND Mismatch Check")

    col_upper_map = {str(c).upper(): c for c in df.columns}

    # --- Resolve parent column for AO-brand grouping + PARENT display ------
    # Prefer raw_parent_col (the dedicated dialog/PL setting); fall back to
    # raw_manufacturer_col for legacy callers that haven't migrated yet.
    parent_col_actual = None
    if raw_parent_col:
        parent_col_actual = col_upper_map.get(raw_parent_col.upper())
    if parent_col_actual is None and raw_manufacturer_col:
        parent_col_actual = col_upper_map.get(raw_manufacturer_col.upper())

    # --- Find all BRAND / TOOL_BRAND column pairs (including suffixed) -----
    suffix_whitelist = (
        {s.upper() for s in valid_model_suffixes} if valid_model_suffixes else None
    )

    def _find_brand_tool_pairs() -> List[tuple]:
        # Discover all TOOL_X / X base pairs (any base name, e.g. BRAND, SUBBRAND)
        # using the same two-pass approach as _find_all_tool_base_pairs in transforms.py.
        pairs: List[tuple] = []
        seen: set = set()
        confirmed_bases: List[str] = []

        # Pass 1: unambiguous base pairs (TOOL_X where X has no underscores)
        for col_key in sorted(col_upper_map.keys()):
            if not col_key.startswith("TOOL_"):
                continue
            base_upper = col_key[len("TOOL_"):]
            if not base_upper or "_" in base_upper:
                continue
            if base_upper not in col_upper_map:
                continue
            base_name = col_upper_map[base_upper]
            tool_name = col_upper_map[col_key]
            pair = (base_name, tool_name)
            if pair not in seen:
                pairs.append((base_name, tool_name, ""))
                seen.add(pair)
                confirmed_bases.append(base_upper)

        # Pass 2: suffixed variants of each confirmed base pair
        for base_upper in confirmed_bases:
            base_prefix = f"{base_upper}_"
            for col_key in col_upper_map:
                if not col_key.startswith(base_prefix):
                    continue
                model_suffix = col_key[len(base_prefix):]
                if not model_suffix:
                    continue
                if suffix_whitelist is not None and model_suffix.upper() not in suffix_whitelist:
                    continue
                tool_candidate = f"TOOL_{base_upper}_{model_suffix}"
                if tool_candidate not in col_upper_map:
                    continue
                brand_name = col_upper_map[col_key]
                tool_name = col_upper_map[tool_candidate]
                pair = (brand_name, tool_name)
                if pair not in seen:
                    pairs.append((brand_name, tool_name, model_suffix))
                    seen.add(pair)

        # Pass 3: when a whitelist is provided, also find TOOL_X_SUFFIX / X_SUFFIX pairs
        # that have no base TOOL_X / X column (projects where only suffixed columns exist).
        if suffix_whitelist:
            for col_key in sorted(col_upper_map.keys()):
                if not col_key.startswith("TOOL_"):
                    continue
                rest = col_key[len("TOOL_"):]  # e.g. "SUBBRAND_MULO"
                for suffix in sorted(suffix_whitelist):
                    suffix_tag = f"_{suffix}"
                    if rest.endswith(suffix_tag) and len(rest) > len(suffix_tag):
                        non_tool = rest  # e.g. "SUBBRAND_MULO"
                        if non_tool in col_upper_map:
                            brand_name = col_upper_map[non_tool]
                            tool_name = col_upper_map[col_key]
                            pair = (brand_name, tool_name)
                            if pair not in seen:
                                pairs.append((brand_name, tool_name, suffix))
                                seen.add(pair)
                        break  # each column matches at most one suffix

        return pairs

    column_pairs = _find_brand_tool_pairs()

    if not column_pairs:
        print(f"{INDENT}No base/TOOL_* column pairs found. Skipping check.")
        return []

    # Log which pairs will be checked
    if len(column_pairs) == 1 and column_pairs[0][2] == "":
        print(f"{INDENT}Checking: {column_pairs[0][0]} vs {column_pairs[0][1]}")
    else:
        model_suffixes = [pair[2] for pair in column_pairs if pair[2]]
        if model_suffixes:
            print(f"{INDENT}Detected model variants: {', '.join(model_suffixes)}")
        print(f"{INDENT}Checking {len(column_pairs)} base/TOOL_* pair(s)")

    total_mismatches = 0
    mismatch_groups: List[Dict[str, Any]] = []

    for brand_column, tool_column, model_suffix in column_pairs:
        brand_values = df[brand_column].astype("string").str.strip().fillna("")
        tool_values = df[tool_column].astype("string").str.strip().fillna("")
        tool_values_upper = tool_values.str.upper()

        # Basic mismatch: BRAND != TOOL_BRAND (case-insensitive)
        mismatch_mask = brand_values.str.upper() != tool_values_upper

        # Exclude rows where both are blank
        both_blank = (brand_values == "") & (tool_values == "")
        mismatch_mask = mismatch_mask & ~both_blank

        mismatch_count = int(mismatch_mask.sum())
        total_mismatches += mismatch_count

        pair_label = (
            f"{brand_column} / {tool_column}"
            if not model_suffix
            else f"{brand_column} / {tool_column} (model={model_suffix})"
        )

        if mismatch_count == 0:
            print(f"{INDENT}{pair_label}: ✓ No mismatches")
            continue

        # Distinct mismatched pairs (using actual column names)
        # When AO brands are present and a parent column is available,
        # include the parent in the grouping so each parent gets its own
        # row — helps spot incorrectly mapped client suffixes.
        include_parent = False
        if parent_col_actual:
            brand_vals_upper = df.loc[mismatch_mask, brand_column].astype(str).str.upper()
            tool_vals_upper = df.loc[mismatch_mask, tool_column].astype(str).str.upper()
            include_parent = (
                brand_vals_upper.str.startswith("AO ").any()
                or tool_vals_upper.str.startswith("AO ").any()
            )

        if include_parent:
            group_cols = [brand_column, tool_column, parent_col_actual]
            rename_map = {brand_column: "BRAND", tool_column: "TOOL_BRAND", parent_col_actual: "PARENT"}
        else:
            group_cols = [brand_column, tool_column]
            rename_map = {brand_column: "BRAND", tool_column: "TOOL_BRAND"}

        distinct_pairs = (
            df.loc[mismatch_mask, group_cols]
            .drop_duplicates()
            .sort_values(group_cols)
            .reset_index(drop=True)
        )

        parent_note = " (incl. parent for AO brands)" if include_parent else ""
        print(f"{INDENT}{pair_label}: ⚠ {mismatch_count} row(s), {len(distinct_pairs)} distinct pair(s){parent_note} — analyst review required")

        # Store group with actual column names for correction targeting
        mismatch_groups.append({
            "model_suffix": model_suffix,
            "brand_col": brand_column,
            "tool_brand_col": tool_column,
            "mismatch_df": distinct_pairs.rename(columns=rename_map),
            "parent_col": parent_col_actual if include_parent else None,
        })

    if total_mismatches == 0:
        print(f"{INDENT}✓ All base/TOOL_* pairs match.")
    else:
        print(f"\n{INDENT}⚠ {total_mismatches} total mismatch row(s) across {len(mismatch_groups)} pair(s) — awaiting user review")

    return mismatch_groups


# ═══════════════════════════════════════════════════════════════════════════
# Step 15: Null Check for Modeling / Reporting Columns
# ═══════════════════════════════════════════════════════════════════════════

def check_null_modeling_reporting_cols(
    df: pd.DataFrame,
    meta_df: pd.DataFrame = None,
    show_step_header: bool = True,
) -> int:
    """
    Flag modeling and reporting columns that contain null or blank values.

    Column classification comes from two sources:
    1. The META sheet (Attribute_Type = MODELING or REPORTING).
    2. Any column ending in ``_RPTG`` is treated as reporting.

    Columns with exactly one distinct non-blank value are auto-filled.
    All other flagged columns are reported with their null count and
    percentage for manual review.

    Returns the number of columns that were auto-filled.
    """
    if show_step_header:
        _print_step_header("15", "Null Check for Modeling/Reporting Columns")

    col_upper_map = {str(c).upper(): c for c in df.columns}

    modeling_columns: set[str] = set()
    reporting_columns: set[str] = set()

    # --- Classify columns from META sheet ----------------------------------
    if meta_df is not None and not meta_df.empty:
        meta_upper_map = {str(c).upper(): c for c in meta_df.columns}
        attr_type_col = meta_upper_map.get("ATTRIBUTE_TYPE")
        attr_group_col = meta_upper_map.get("ATTRIBUTE GROUP NAME")

        if attr_type_col and attr_group_col:
            for _, meta_row in meta_df.iterrows():
                attribute_type = str(meta_row[attr_type_col]).strip().upper()
                attribute_group = str(meta_row[attr_group_col]).strip()

                actual_col = col_upper_map.get(attribute_group.upper())
                if actual_col:
                    if attribute_type == "MODELING":
                        modeling_columns.add(actual_col)
                    elif attribute_type == "REPORTING":
                        reporting_columns.add(actual_col)
        else:
            print(f"{INDENT}META tab missing required columns (Attribute_Type, Attribute Group name)")

    # --- Also treat *_RPTG columns as reporting ----------------------------
    for col_key, actual_col in col_upper_map.items():
        if col_key.endswith("RPTG") or col_key.endswith("_RPTG"):
            reporting_columns.add(actual_col)

    all_target_columns = modeling_columns | reporting_columns
    if not all_target_columns:
        print(f"{INDENT}No modeling/reporting columns identified. Skipping check.")
        return 0

    print(f"{INDENT}Found {len(modeling_columns)} modeling column(s), {len(reporting_columns)} reporting column(s)")
    print(f"{INDENT}Total columns to check: {len(all_target_columns)}")

    # --- Check each column for nulls / blanks / NaN text -------------------
    total_rows = len(df)
    null_report_rows: List[Dict[str, Any]] = []
    auto_filled_count = 0

    _NAN_TEXT_VALUES = {"NAN", "NONE", "NULL", "N/A", "NA"}

    for col_name in sorted(all_target_columns):
        # Determine column classification label
        if col_name in modeling_columns and col_name in reporting_columns:
            column_type = "MODELING/RPTG"
        elif col_name in modeling_columns:
            column_type = "MODELING"
        else:
            column_type = "REPORTING"

        column_series = df[col_name]

        # Numeric columns: only check for NaN/null
        if is_integer_dtype(column_series) or is_float_dtype(column_series):
            null_mask = column_series.isna()
            null_count = int(null_mask.sum())
            nan_text_count = 0
            non_blank_series = column_series.dropna()
        else:
            # String columns: check nulls, blanks, and literal "NaN" text
            stripped_series = column_series.astype("string").str.strip()
            null_blank_mask = stripped_series.isna() | (stripped_series == "")
            nan_text_mask = stripped_series.str.upper().isin(_NAN_TEXT_VALUES)
            null_mask = null_blank_mask | nan_text_mask
            null_count = int(null_blank_mask.sum())
            nan_text_count = int((nan_text_mask & ~null_blank_mask).sum())
            non_blank_series = stripped_series[~null_mask].dropna()

        total_null = null_count + nan_text_count
        if total_null == 0:
            continue

        percent_missing = (total_null / total_rows * 100) if total_rows > 0 else 0.0

        # Auto-fill columns with exactly one distinct non-blank value
        action = ""
        if len(non_blank_series) > 0 and non_blank_series.nunique() == 1:
            fill_value = non_blank_series.iloc[0]
            df.loc[null_mask, col_name] = fill_value
            action = f"Auto-filled → {fill_value}"
            auto_filled_count += 1
            print(f"{INDENT}  {col_name}: auto-filled {total_null} null(s) with '{fill_value}' (only value in column)")

        row_entry: Dict[str, Any] = {
            "Column": col_name,
            "Type": column_type,
            "Nulls": total_null,
            "% Missing": f"{percent_missing:.1f}%",
        }
        if nan_text_count > 0:
            row_entry["NaN Text"] = nan_text_count
        if action:
            row_entry["Action"] = action
        null_report_rows.append(row_entry)

    if not null_report_rows:
        print(f"{INDENT}✓ All modeling/reporting columns have complete data (no nulls).")
        return 0

    null_report_df = pd.DataFrame(null_report_rows).sort_values(
        ["Nulls", "Column"], ascending=[False, True], ignore_index=True
    )

    print(f"\n{INDENT}⚠ {len(null_report_df)} column(s) contain null/blank values:")
    _print_df(null_report_df, indent=INDENT + "  ")

    if auto_filled_count:
        print(f"\n{INDENT}✓ Auto-filled {auto_filled_count} column(s) where only a single value existed.")

    return auto_filled_count


# ═══════════════════════════════════════════════════════════════════════════
# Step 17: RAW_ASSORTMENT_CATEGORY Split
# ═══════════════════════════════════════════════════════════════════════════

def split_by_raw_assortment_category(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    Split the final DataFrame by RAW_ASSORTMENT_CATEGORY into separate
    DataFrames (one per category), used as individual Excel sheets.
    """
    col_upper_map = {str(c).upper(): c for c in df.columns}
    category_col = col_upper_map.get("RAW_ASSORTMENT_CATEGORY")

    if not category_col:
        print(f"{INDENT}Column 'RAW_ASSORTMENT_CATEGORY' is MISSING — treating all rows as a single category.")
        return {"_NO_CATEGORY_": df}

    category_series = df[category_col]
    has_blanks = (
        category_series.isna().any()
        or (category_series.astype(str).str.strip() == "").any()
    )

    # Get sorted list of unique non-blank category values
    unique_categories = (
        category_series.dropna()
        .astype(str)
        .str.strip()
        .loc[lambda s: s != ""]
        .unique()
        .tolist()
    )
    unique_categories = sorted(unique_categories)

    print(f"{INDENT}Blank values present:  {has_blanks}")
    print(f"{INDENT}Unique categories ({len(unique_categories)}):")

    # Build category → DataFrame mapping
    category_splits: dict[str, pd.DataFrame] = {}
    for category_name in unique_categories:
        category_subset = df[category_series.astype(str).str.strip() == category_name].copy()
        # Excel sheet names are limited to 31 chars; also sanitize path separators
        safe_sheet_name = category_name[:30].replace("/", "_").replace("\\", "_")
        category_splits[safe_sheet_name] = category_subset
        print(f"{INDENT}  {category_name}: {len(category_subset)} row(s)")

    # Capture blank-category rows separately
    if has_blanks:
        blank_rows = df[category_series.astype(str).str.strip() == ""]
        if not blank_rows.empty:
            category_splits["_BLANK_CATEGORY_"] = blank_rows
            print(f"{INDENT}  _BLANK_CATEGORY_: {len(blank_rows)} row(s)")

    print(f"\n{INDENT}Split into {len(category_splits)} category group(s) for export.")

    return category_splits
