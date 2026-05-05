"""
SKU collapse logic for Phase 3 pipeline (Step 14).

Handles scientific-notation SKU cleanup and row-wise collapsing of duplicate SKU groups.

Collapse modes
--------------
- Normal (top-dollar): the row with the highest RAW_TOTAL_DOLLARS within each SKU group
  is treated as the "parent" and its non-RAW column values are propagated to all other
  rows in the group.

- Custom (parent dim-key): the analyst designates a parent row by setting
  ITEM_DIM_KEY == SKU. If no explicit parent is found, the first row in the group is
  used as a fallback and a warning is logged.

Pipeline summary
----------------
1) Detect scientific-notation SKUs and replace them using DESCRIPTION-derived pseudo SKUs.
2) Pre-collapse auto-heal: if the same DESCRIPTION maps to multiple SKUs, derive SKU from DESCRIPTION,
   show grouped before/after examples, and validate the anomaly is removed.
3) Collapse duplicate SKUs (top-dollar or custom-parent), propagating non-RAW attributes (never overwriting ITEM_DIM_KEY).
"""

from __future__ import annotations

import re
import pandas as pd


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
    """Print a consistently formatted step header.

    Leading double newline keeps each step visually separated from the
    previous step's output instead of abutting it.
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


# ═══════════════════════════════════════════════════════════════════════════
# Scientific Notation Helpers
# ═══════════════════════════════════════════════════════════════════════════

_SCIENTIFIC_NOTATION_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)[eE][+-]?\d+$")


def _is_scientific_notation(value) -> bool:
    """Return True if *value* looks like scientific notation (e.g. '1.2e+05')."""
    if pd.isna(value):
        return False
    return bool(_SCIENTIFIC_NOTATION_RE.match(str(value).strip()))


def _extract_pseudo_sku_from_description(desc) -> str:
    """
    Extract the portion of DESCRIPTION after the first '- ' separator.

    If '- ' is not present, returns the full description trimmed.
    If desc is null/blank, returns ''.
    """
    if pd.isna(desc):
        return ""
    text = str(desc).strip()
    if not text:
        return ""
    parts = text.split("- ", 1)  # split once
    if len(parts) == 2:
        return parts[1].strip()
    return text


def _build_pseudo_sku_mapping(
    df: pd.DataFrame,
    description_col: str = "DESCRIPTION",
    id_col: str = "ITEM_DIM_KEY",
    output_col: str = "PSEUDO SKU",
) -> pd.DataFrame:
    """
    Build a mapping from ITEM_DIM_KEY → pseudo SKU (derived from DESCRIPTION).

    Returns a two-column DataFrame: [id_col, output_col].
    """
    col_upper_map = {c.upper(): c for c in df.columns}
    description_col_resolved = col_upper_map.get(description_col.upper(), description_col)
    id_col_resolved = col_upper_map.get(id_col.upper(), id_col)

    return pd.DataFrame(
        {
            id_col: df[id_col_resolved],
            output_col: df[description_col_resolved].apply(_extract_pseudo_sku_from_description),
        }
    )


def _replace_sci_skus_with_pseudo(
    sci_notation_df: pd.DataFrame,
    mapping_df: pd.DataFrame,
    id_col: str = "ITEM_DIM_KEY",
    pseudo_col: str = "PSEUDO SKU",
    sku_col: str = "SKU",
) -> pd.DataFrame:
    """Replace scientific-notation SKUs using the id → pseudo SKU mapping."""
    col_upper_map = {c.upper(): c for c in sci_notation_df.columns}
    id_col_resolved = col_upper_map.get(id_col.upper(), id_col)
    sku_col_resolved = col_upper_map.get(sku_col.upper(), sku_col)

    id_to_pseudo = dict(zip(mapping_df[id_col], mapping_df[pseudo_col]))
    result = sci_notation_df.copy()
    result[sku_col_resolved] = (
        result[id_col_resolved].map(id_to_pseudo).fillna(result[sku_col_resolved])
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Pre-collapse auto-heal: DESCRIPTION → multiple SKUs
# ═══════════════════════════════════════════════════════════════════════════

def _autoheal_desc_multi_sku_pre_collapse(
    df: pd.DataFrame,
    *,
    desc_col: str,
    sku_col: str,
    verbose: bool = True,
    example_groups: int = 2,
) -> pd.DataFrame:
    """
    Pre-collapse DESCRIPTION→SKU auto-heal:

    - Finds DESCRIPTION values that map to >1 SKU.
    - Sets SKU from DESCRIPTION (strictly: only if '- ' delimiter exists).
    - Prints up to `example_groups` grouped examples showing SKU_BEFORE and SKU_AFTER.
    - Validates after the transformation that DESCRIPTION→multiple-SKU returns False.

    NOTE: This function intentionally does NOT run perform_sku_collapse().
          Call perform_sku_collapse() once afterwards on the full DataFrame.
    """

    def _extract_sku_from_description_strict(desc) -> str:
        """Return substring after first '- ' or '' if delimiter missing."""
        if pd.isna(desc):
            return ""
        text = str(desc).strip()
        if not text or "- " not in text:
            return ""
        return text.split("- ", 1)[1].strip()

    # Identify anomalies: same DESCRIPTION -> multiple SKUs
    desc_series = df[desc_col].astype("string").str.strip()
    sku_series = df[sku_col].astype("string").str.strip()
    valid_rows = desc_series.ne("").fillna(False) & sku_series.ne("").fillna(False)

    skus_per_description = (
        df.loc[valid_rows, [desc_col, sku_col]]
        .drop_duplicates()
        .groupby(desc_col)[sku_col]
        .nunique()
    )
    multi_sku_descs = skus_per_description[skus_per_description >= 2].index

    if len(multi_sku_descs) == 0:
        if verbose:
            print(f"\n{INDENT}Description→SKU clean: ✓ no anomalies (no DESCRIPTION maps to multiple SKUs).")
        return df

    if verbose:
        print(f"\n{INDENT}Description→SKU clean: ⚠ {len(multi_sku_descs)} DESCRIPTION(s) map to multiple SKUs.")
        print(f"{INDENT}Auto-fix: set SKU from DESCRIPTION (post '- ') before collapse.\n")

    # Rows affected by anomaly
    fix_mask = valid_rows & desc_series.isin(multi_sku_descs)

    # Derive SKU from DESCRIPTION (strict)
    derived_sku = df.loc[fix_mask, desc_col].apply(_extract_sku_from_description_strict)

    # Only apply where derived sku is non-empty
    apply_mask = fix_mask.copy()
    apply_mask.loc[fix_mask] = derived_sku.astype("string").str.strip().ne("").fillna(False)

    if verbose:
        print(f"{INDENT}Auto-fix candidates: {int(fix_mask.sum())} row(s)")
        print(f"{INDENT}Auto-fix updates (derived SKU present): {int(apply_mask.sum())} row(s)")

    if int(apply_mask.sum()) == 0:
        if verbose:
            print(f"{INDENT}No rows had a derivable SKU from DESCRIPTION; leaving as-is.")
        return df

    # Print grouped examples AFTER (same DESCRIPTION)
    if verbose and example_groups > 0:
        sample_descs = list(pd.Index(multi_sku_descs).tolist())[: max(1, example_groups)]
        for i, sample_desc in enumerate(sample_descs, start=1):
            group_mask = df[desc_col].astype("string").str.strip().eq(str(sample_desc).strip())
            group_before = df.loc[group_mask, [desc_col, sku_col]].copy()
            group_before = group_before.sort_values(by=[sku_col], ascending=[True]).drop_duplicates()

            group_after = group_before.copy()
            group_after.rename(columns={sku_col: "SKU_BEFORE"}, inplace=True)
            group_after["SKU_AFTER"] = group_after[desc_col].apply(_extract_sku_from_description_strict)
            group_after["SKU_AFTER"] = group_after["SKU_AFTER"].astype("string").str.strip()
            group_after.loc[group_after["SKU_AFTER"].eq("").fillna(True), "SKU_AFTER"] = (
                group_after["SKU_BEFORE"].astype("string").str.strip()
            )

            print(f"\n{INDENT}Example group {i}: SKU derived from DESCRIPTION (AFTER)")
            _print_df(group_after[[desc_col, "SKU_BEFORE", "SKU_AFTER"]], indent=INDENT + "  ")

    # Apply SKU corrections
    df_fixed = df.copy()
    df_fixed.loc[apply_mask, sku_col] = derived_sku.loc[apply_mask].values

    # Validation: same DESCRIPTION maps to multiple SKUs should be FALSE
    desc_series2 = df_fixed[desc_col].astype("string").str.strip()
    sku_series2 = df_fixed[sku_col].astype("string").str.strip()
    valid_rows2 = desc_series2.ne("").fillna(False) & sku_series2.ne("").fillna(False)

    skus_per_description2 = (
        df_fixed.loc[valid_rows2, [desc_col, sku_col]]
        .drop_duplicates()
        .groupby(desc_col)[sku_col]
        .nunique()
    )
    has_multi_after = bool((skus_per_description2 >= 2).any())

    if verbose:
        if has_multi_after:
            remaining = int((skus_per_description2 >= 2).sum())
            print(f"\n{INDENT}Validation: ✗ FAIL — {remaining} DESCRIPTION(s) still map to multiple SKUs.")
        else:
            print(f"\n{INDENT}Validation: ✓ PASS — DESCRIPTION→multiple-SKU check returns False.")

    return df_fixed


# ═══════════════════════════════════════════════════════════════════════════
# Step 14: SKU Collapse — Full Pipeline Entry Point
# ═══════════════════════════════════════════════════════════════════════════

def prepare_and_collapse(
    df: pd.DataFrame,
    *,
    desc_col: str = "DESCRIPTION",
    sku_col: str = "SKU",
    id_col: str = "ITEM_DIM_KEY",
    dollars_col: str = "RAW_TOTAL_DOLLARS",
    output_filepath: str | None = None,
    verbose: bool = True,
    is_custom_collapse: bool = False,
    show_step_header: bool = True,
) -> pd.DataFrame:
    """
    Full SKU collapse flow:

    1. Detect scientific-notation SKUs.
    2. Split into sci-notation vs normal rows.
    3. Build pseudo SKUs from DESCRIPTION, replace in sci-notation rows.
    4. Recombine and verify no sci-notation remains.
    5. Pre-collapse auto-heal DESCRIPTION→multiple SKUs (derive SKU from DESCRIPTION + validation).
    6. Collapse duplicate SKU groups (top-dollar or custom parent).
    """
    if show_step_header:
        _print_step_header("14", "SKU Collapse Implementation")

    # Resolve column names (case-insensitive)
    col_upper_map = {c.upper(): c for c in df.columns}
    for col_label, col_value in [
        ("Description", desc_col),
        ("SKU", sku_col),
        ("ID", id_col),
        ("Dollars", dollars_col),
    ]:
        if col_value.upper() not in col_upper_map:
            raise ValueError(f"{col_label} column '{col_value}' not found in DataFrame.")

    desc_col = col_upper_map[desc_col.upper()]
    sku_col = col_upper_map[sku_col.upper()]
    id_col = col_upper_map[id_col.upper()]
    dollars_col = col_upper_map[dollars_col.upper()]

    # --- Step 1-2: Detect and split scientific notation SKUs ---------------
    sci_mask = df[sku_col].apply(_is_scientific_notation)
    sci_notation_df = df[sci_mask].reset_index(drop=True)
    normal_df = df[~sci_mask].reset_index(drop=True)

    # --- Step 3: Replace sci-notation SKUs with pseudo SKUs ----------------
    pseudo_mapping = _build_pseudo_sku_mapping(
        df,
        description_col=desc_col,
        id_col=id_col,
    )
    fixed_sci_df = _replace_sci_skus_with_pseudo(
        sci_notation_df,
        mapping_df=pseudo_mapping,
        id_col=id_col,
        pseudo_col="PSEUDO SKU",
        sku_col=sku_col,
    )

    # --- Step 4: Recombine and verify -------------------------------------
    combined_df = pd.concat([normal_df, fixed_sci_df], ignore_index=True)

    remaining_sci = combined_df[sku_col].apply(_is_scientific_notation)
    if verbose:
        print(f"{INDENT}Scientific notation present: {remaining_sci.unique()}")
    if remaining_sci.any():
        raise ValueError("Error: Scientific Notation still present in SKU col")

    # --- Step 5: Pre-collapse auto-heal (examples + validation) ------------
    combined_df = _autoheal_desc_multi_sku_pre_collapse(
        combined_df,
        desc_col=desc_col,
        sku_col=sku_col,
        verbose=verbose,
        example_groups=2,
    )

    # --- Pre-collapse summary ---------------------------------------------
    sku_counts = combined_df[sku_col].value_counts(dropna=False)
    rows_to_update = int((sku_counts - 1).clip(lower=0).sum())
    duplicate_sku_count = int((sku_counts > 1).sum())
    max_group_size = int(sku_counts.max()) if not sku_counts.empty else 0
    pairs_count = int((sku_counts == 2).sum())
    triples_count = int((sku_counts == 3).sum())
    four_plus_count = int((sku_counts >= 4).sum())

    if verbose:
        if rows_to_update == 0:
            print(f"{INDENT}Pre-collapse summary: 0 rows to update — no duplicate SKUs detected.")
        else:
            print(
                f"{INDENT}Pre-collapse summary: {rows_to_update} rows across "
                f"{duplicate_sku_count} duplicate SKUs flagged for updating."
            )
            print(f"{INDENT}  • {pairs_count} SKUs with 2 rows")
            if triples_count > 0:
                print(f"{INDENT}  • {triples_count} SKU(s) with 3 rows")
            if four_plus_count > 0:
                print(f"{INDENT}  • {four_plus_count} SKU(s) with ≥4 rows (max group size: {max_group_size})")

    # --- Step 6: Collapse -------------------------------------------------
    collapsed_df = perform_sku_collapse(
        combined_df,
        output_filepath=output_filepath,
        sku_col=sku_col,
        dollars_col=dollars_col,
        id_col=id_col,
        verbose=verbose,
        is_custom_collapse=is_custom_collapse,
    )

    return collapsed_df


# ═══════════════════════════════════════════════════════════════════════════
# Core SKU Collapse
# ═══════════════════════════════════════════════════════════════════════════

def perform_sku_collapse(
    df: pd.DataFrame,
    output_filepath: str | None = None,
    *,
    sku_col: str = "SKU",
    dollars_col: str = "RAW_TOTAL_DOLLARS",
    id_col: str = "ITEM_DIM_KEY",
    verbose: bool = True,
    is_custom_collapse: bool = False,
) -> pd.DataFrame:
    """
    Row-wise collapse within each SKU group.

    Propagates non-RAW column values from the "parent" row to all other
    rows in the same SKU group. ITEM_DIM_KEY is never overwritten.

    Parameters
    ----------
    is_custom_collapse : bool
        If False (default): parent = row with highest RAW_TOTAL_DOLLARS.
        If True: parent = row where ITEM_DIM_KEY == SKU.
    """
    col_upper_map = {c.upper(): c for c in df.columns}

    if sku_col.upper() not in col_upper_map:
        raise ValueError(f"SKU column '{sku_col}' not found in DataFrame.")
    sku_col = col_upper_map[sku_col.upper()]

    if id_col.upper() not in col_upper_map:
        raise ValueError(f"ID column '{id_col}' not found in DataFrame.")
    id_col = col_upper_map[id_col.upper()]

    if not is_custom_collapse:
        if dollars_col.upper() not in col_upper_map:
            raise ValueError(f"Dollars column '{dollars_col}' not found in DataFrame.")
        dollars_col = col_upper_map[dollars_col.upper()]

    # Sort: by SKU (+ descending dollars for top-dollar mode)
    if is_custom_collapse:
        sorted_df = df.sort_values(by=[sku_col], ascending=[True]).reset_index(drop=True)
    else:
        sorted_df = (
            df.sort_values(by=[sku_col, dollars_col], ascending=[True, False])
            .reset_index(drop=True)
        )

    sku_counts = sorted_df[sku_col].value_counts(dropna=False)
    rows_to_update = int((sku_counts - 1).clip(lower=0).sum())
    duplicate_sku_count = int((sku_counts > 1).sum())

    if rows_to_update == 0:
        if verbose:
            print(f"{INDENT}✓ SKU collapse: no duplicate SKUs detected — no updates needed.")
        if output_filepath:
            sorted_df.to_excel(output_filepath, index=False)
            if verbose:
                print(f"{INDENT}Output saved to {output_filepath}")
        return sorted_df

    if verbose:
        collapse_mode = (
            "custom (analyst-selected parent via ITEM_DIM_KEY == SKU)"
            if is_custom_collapse
            else "standard (top-dollar row per SKU)"
        )
        print(f"{INDENT}SKU collapse mode: {collapse_mode}")
        print(
            f"{INDENT}Propagating 'parent' row values to {rows_to_update} rows across "
            f"{duplicate_sku_count} SKUs."
        )

    # Determine which columns to propagate (non-RAW, non-ID)
    non_raw_columns = [c for c in sorted_df.columns if not str(c).upper().startswith("RAW")]
    columns_to_copy = [c for c in non_raw_columns if c != id_col]

    # Track custom collapse validation (only in custom mode)
    if is_custom_collapse:
        skus_with_explicit_parent = 0
        skus_missing_parent: list = []

    for sku_value, group_indices in sorted_df.groupby(sku_col, sort=False).groups.items():
        index_list = list(group_indices)
        if len(index_list) <= 1:
            continue

        group_df = sorted_df.loc[index_list]

        if is_custom_collapse:
            parent_match = group_df[id_col].astype(str) == group_df[sku_col].astype(str)
            if parent_match.any():
                parent_index = group_df.index[parent_match][0]
                skus_with_explicit_parent += 1
            else:
                skus_missing_parent.append(sku_value)
                parent_index = index_list[0]
        else:
            parent_index = index_list[0]

        child_indices = [idx for idx in index_list if idx != parent_index]
        if child_indices:
            sorted_df.loc[child_indices, columns_to_copy] = sorted_df.loc[parent_index, columns_to_copy].values

    if is_custom_collapse and verbose:
        total_multi_row_skus = skus_with_explicit_parent + len(skus_missing_parent)
        if skus_missing_parent:
            print(
                f"{INDENT}Custom collapse validation: {skus_with_explicit_parent}/{total_multi_row_skus} "
                f"SKU groups have explicit parent (ITEM_DIM_KEY == SKU)."
            )
            print(
                f"{INDENT}⚠ WARNING: {len(skus_missing_parent)} SKU group(s) missing parent match "
                f"(using first row as fallback):"
            )
            for missing_sku in skus_missing_parent:
                print(f"{INDENT}  • {missing_sku}")
        else:
            print(
                f"{INDENT}✓ Custom collapse validation: all {total_multi_row_skus} SKU groups "
                f"have explicit parent (ITEM_DIM_KEY == SKU)."
            )

    if verbose:
        print(f"{INDENT}✓ SKU collapse complete.")

    if output_filepath:
        sorted_df.to_excel(output_filepath, index=False)
        if verbose:
            print(f"{INDENT}Output saved to {output_filepath}")

    return sorted_df
