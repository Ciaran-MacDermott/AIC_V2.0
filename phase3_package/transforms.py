"""
Phase 3 data transformation functions.

Each function corresponds to a numbered pipeline step and modifies the
DataFrame in place (returning the updated copy).  All transformations
support multi-model column variants (e.g. TOOL_BRAND, TOOL_BRAND_MULO,
TOOL_BRAND_CONV) via case-insensitive suffix discovery.

Step Index (matching pipeline.py)
---------------------------------
 4   overwrite_upc10_for_private_label – Replace UPC10 with ITEM_DIM_KEY for PL rows.
 5   apply_private_label_rules         – Tag PL rows by retailer (Walmart, CVS, HEB).
 7    normalize_upc10                   – Left-pad UPC10 to 10 chars, mirror to UPC10_ATTR.
10.5 strip_legacy_restricted_suffix    – Remove stale RESTRICTED suffixes before re-tagging.
10.6 strip_legacy_brand_overrides     – Reset stale brand overrides for non-configured manufacturers.
11   apply_brand_overrides             – Client manufacturer → brand mapping overrides.
12   raw_multi_restricted_overrides    – Canonicalize TOOL_BRAND with _RESTRICTED.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import pandas as pd
import regex as re


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
    """Print a consistently formatted step header (e.g. ``4) Private Label UPC10 Overwrite``)."""
    print(f"\n{step}) {title}")
    print(MINOR_SEP)


# ---------------------------------------------------------------------------
# Log-friendly DataFrame printing
# ---------------------------------------------------------------------------

def _truncate_cell(value: Any, max_length: int) -> str:
    """Truncate a cell value for log output, replacing newlines/tabs with spaces."""
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if max_length is None or max_length <= 0:
        return text
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return "." * max_length
    return text[: max_length - 3] + "..."


def _print_df(
    df: pd.DataFrame,
    *,
    title: str | None = None,
    indent: str = INDENT,
    max_rows: int | None = None,
    min_col_width: int = 12,
    col_padding: int = 2,
    max_col_width: int = 48,
) -> None:
    """
    Pretty-print a DataFrame for log output.

    Drops duplicate rows so "example" output only shows distinct values.
    Column alignment: first=left, middle=center, last=right.
    Cell values are truncated to *max_col_width* to prevent wide tables.
    """
    if title:
        print(_indent_block(title, indent=indent))
    if df is None:
        print(_indent_block("(None)", indent=indent))
        return
    if df.empty:
        print(_indent_block("(no rows)", indent=indent))
        return

    view = df.drop_duplicates().copy()
    if max_rows is not None:
        view = view.head(max_rows)

    columns = list(view.columns)
    num_cols = len(columns)

    def _alignment_for(col_index: int) -> str:
        """Left-align first column, right-align last, center the rest."""
        if num_cols == 1 or col_index == 0:
            return "<"
        if col_index == num_cols - 1:
            return ">"
        return "^"

    column_separator = " | "

    # Stringify and truncate all cell values before computing widths
    string_df = view.copy()
    for col_name in columns:
        series = string_df[col_name].astype("string").fillna("").astype(str)
        series = series.str.replace(r"[\r\n\t]+", " ", regex=True)
        series = series.map(lambda x: _truncate_cell(x, max_col_width))
        string_df[col_name] = series

    # Compute column widths from header + cell values
    col_widths: dict[str, int] = {}
    for col_name in columns:
        header_width = len(str(col_name))
        max_value_width = max([len(v) for v in string_df[col_name].tolist()], default=0)
        col_widths[col_name] = max(header_width, max_value_width, min_col_width) + col_padding

    def _format_cell(text: str, width: int, alignment: str) -> str:
        return f"{text:{alignment}{width}}"

    # Build header row
    header_cells = [
        _format_cell(str(col_name), col_widths[col_name], _alignment_for(i))
        for i, col_name in enumerate(columns)
    ]
    header_line = column_separator.join(header_cells)

    # Build separator line
    separator_line = column_separator.join("-" * col_widths[col_name] for col_name in columns)

    # Build data rows
    data_lines = [header_line, separator_line]
    for _, row in string_df.iterrows():
        row_cells = [
            _format_cell(str(row[col_name]), col_widths[col_name], _alignment_for(i))
            for i, col_name in enumerate(columns)
        ]
        data_lines.append(column_separator.join(row_cells))

    print(_indent_block("\n".join(data_lines), indent=indent))


# ═══════════════════════════════════════════════════════════════════════════
# Column Variant Discovery (shared helper)
# ═══════════════════════════════════════════════════════════════════════════

def _find_tool_brand_variants(
    df: pd.DataFrame,
    base_col: str,
    valid_suffixes: Optional[set] = None,
) -> List[tuple]:
    """
    Find the base column and any suffixed variants (e.g. TOOL_BRAND_MULO).

    When *valid_suffixes* is provided, only suffixes present in the set
    are included (matched case-insensitively).  Base columns always pass.

    Returns a list of ``(actual_column_name, suffix_string)`` tuples.
    The base column (if found) has suffix ``""``.
    """
    col_upper_map = {c.upper(): c for c in df.columns}
    base_upper = base_col.upper()
    variants: List[tuple] = []
    seen: set = set()

    # Normalise the whitelist once for O(1) lookups
    suffix_whitelist = {s.upper() for s in valid_suffixes} if valid_suffixes else None

    # Base column
    if base_upper in col_upper_map:
        actual_name = col_upper_map[base_upper]
        variants.append((actual_name, ""))
        seen.add(actual_name)

    # Suffixed variants (e.g. TOOL_BRAND_MULO, TOOL_BRAND_CONV)
    prefix_upper = f"{base_upper}_"
    for col_key, actual_name in col_upper_map.items():
        if col_key.startswith(prefix_upper) and actual_name not in seen:
            model_suffix = col_key[len(prefix_upper):]
            if suffix_whitelist is not None and model_suffix.upper() not in suffix_whitelist:
                continue
            variants.append((actual_name, model_suffix))
            seen.add(actual_name)

    return variants


def _find_brand_tool_pairs(
    df: pd.DataFrame,
    brand_base: str = "BRAND",
    tool_base: str = "TOOL_BRAND",
    valid_suffixes: Optional[set] = None,
) -> List[tuple]:
    """
    Find paired BRAND / TOOL_BRAND columns including suffixed variants.

    When *valid_suffixes* is provided, only suffixes present in the set
    are included (matched case-insensitively).  Base pairs always pass.

    Returns list of ``(brand_col, tool_col, suffix)`` tuples.
    """
    col_upper_map = {c.upper(): c for c in df.columns}
    brand_upper = brand_base.upper()
    tool_upper = tool_base.upper()

    # Normalise the whitelist once for O(1) lookups
    suffix_whitelist = {s.upper() for s in valid_suffixes} if valid_suffixes else None

    pairs: List[tuple] = []
    seen: set = set()

    # Base pair (no suffix)
    if brand_upper in col_upper_map and tool_upper in col_upper_map:
        brand_name = col_upper_map[brand_upper]
        tool_name = col_upper_map[tool_upper]
        pair = (brand_name, tool_name, "")
        pairs.append(pair)
        seen.add(pair)

    # Suffixed variants (e.g. BRAND_MULO / TOOL_BRAND_MULO)
    brand_prefix = f"{brand_upper}_"
    for col_key in col_upper_map:
        if col_key.startswith(brand_prefix):
            model_suffix = col_key[len(brand_prefix):]
            if suffix_whitelist is not None and model_suffix.upper() not in suffix_whitelist:
                continue
            tool_candidate = f"{tool_upper}_{model_suffix}"
            if tool_candidate in col_upper_map:
                brand_name = col_upper_map[col_key]
                tool_name = col_upper_map[tool_candidate]
                pair = (brand_name, tool_name, model_suffix)
                if pair not in seen:
                    pairs.append(pair)
                    seen.add(pair)

    return pairs


def _find_all_tool_base_pairs(
    df: pd.DataFrame,
    valid_suffixes: Optional[set] = None,
) -> List[tuple]:
    """
    Discover all ``(base_col, tool_col, suffix)`` pairs where ``TOOL_X`` and
    ``X`` both exist in *df*, for **any** base name ``X``
    (e.g. BRAND, SUBBRAND, MKTBRAND).

    Pass 1 – identify unambiguous base pairs: ``TOOL_X`` columns whose
    stripped name ``X`` contains no underscores and has a matching column.

    Pass 2 – append suffixed variants of each confirmed base pair
    (e.g. ``BRAND_MULO`` / ``TOOL_BRAND_MULO``).

    When *valid_suffixes* is provided, only suffixes present in the set
    are included (base pairs always pass).  Returns
    ``(base_col, tool_col, suffix)`` tuples; suffix is ``""`` for base pairs.
    """
    col_upper_map = {c.upper(): c for c in df.columns}
    suffix_whitelist = {s.upper() for s in valid_suffixes} if valid_suffixes else None

    pairs: List[tuple] = []
    seen: set = set()
    confirmed_bases: List[tuple] = []  # (base_upper, tool_col_key) for pass 2

    # Pass 1: TOOL_X / X base pairs (X must have no underscores to be unambiguous)
    for col_key in sorted(col_upper_map.keys()):
        if not col_key.startswith("TOOL_"):
            continue
        base_upper = col_key[len("TOOL_"):]
        if not base_upper or "_" in base_upper:
            continue  # skip TOOL_BRAND_MULO etc. — handled in pass 2
        if base_upper not in col_upper_map:
            continue
        base_name = col_upper_map[base_upper]
        tool_name = col_upper_map[col_key]
        pair = (base_name, tool_name)
        if pair not in seen:
            pairs.append((base_name, tool_name, ""))
            seen.add(pair)
            confirmed_bases.append((base_upper, col_key))

    # Pass 2: suffixed variants of each confirmed base pair
    for base_upper, _ in confirmed_bases:
        base_prefix = f"{base_upper}_"
        for other_key in col_upper_map:
            if not other_key.startswith(base_prefix):
                continue
            suffix = other_key[len(base_prefix):]
            if not suffix:
                continue
            if suffix_whitelist is not None and suffix not in suffix_whitelist:
                continue
            tool_candidate = f"TOOL_{base_upper}_{suffix}"
            if tool_candidate not in col_upper_map:
                continue
            b_name = col_upper_map[other_key]
            t_name = col_upper_map[tool_candidate]
            pair = (b_name, t_name)
            if pair not in seen:
                pairs.append((b_name, t_name, suffix))
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
                        b_name = col_upper_map[non_tool]
                        t_name = col_upper_map[col_key]
                        pair = (b_name, t_name)
                        if pair not in seen:
                            pairs.append((b_name, t_name, suffix))
                            seen.add(pair)
                    break  # each column matches at most one suffix

    return pairs


# ═══════════════════════════════════════════════════════════════════════════
# Step 4: Private Label UPC10 Overwrite
# ═══════════════════════════════════════════════════════════════════════════

def overwrite_upc10_for_private_label(
    df: pd.DataFrame,
    raw_upc_pl_brand_col: str = "RAW_BRAND",
    upc_col: str = "UPC10",
    key_col: str = "ITEM_DIM_KEY",
    show_examples: bool = True,
) -> pd.DataFrame:
    """
    For rows where the RAW brand column contains 'private label',
    overwrite UPC10 with the ITEM_DIM_KEY value.
    """
    _print_step_header("4", "Private Label UPC10 Overwrite Check")

    col_upper_map = {c.upper(): c for c in df.columns}

    # Resolve all required columns (case-insensitive)
    required_columns = [raw_upc_pl_brand_col, upc_col, key_col]
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for col_name in required_columns:
        if col_name.upper() in col_upper_map:
            resolved[col_name] = col_upper_map[col_name.upper()]
        else:
            missing.append(col_name)

    if missing:
        print(f"{INDENT}Missing required columns: {missing}. No changes made.")
        return df

    raw_upc_pl_brand_col = resolved[raw_upc_pl_brand_col]
    upc_col = resolved[upc_col]
    key_col = resolved[key_col]

    # Identify private label rows
    raw_brand_series = df[raw_upc_pl_brand_col].astype("string")
    is_private_label = raw_brand_series.str.contains("private label", case=False, na=False)
    total_pl_rows = int(is_private_label.sum())
    print(f"{INDENT}Rows where '{raw_upc_pl_brand_col}' contains 'private label': {total_pl_rows} of {len(df)}")

    if total_pl_rows == 0:
        print(f"{INDENT}✓ No rows to update.")
        return df

    # Only update rows where UPC10 differs from ITEM_DIM_KEY
    upc_series = df[upc_col].astype("string")
    key_series = df[key_col].astype("string")
    already_equal = upc_series.eq(key_series)
    update_mask = is_private_label & ~already_equal
    rows_to_change = int(update_mask.sum())

    if rows_to_change == 0:
        print(f"{INDENT}All matched rows already equal; no changes made.")
        return df

    # Capture before values for audit trail, then apply overwrite
    upc_before = upc_series[update_mask].copy()
    key_values = key_series[update_mask].copy()
    df.loc[update_mask, upc_col] = df.loc[update_mask, key_col]
    print(f"{INDENT}✓ Replaced '{upc_col}' with '{key_col}' for {rows_to_change} rows.")

    if show_examples:
        upc_after = df.loc[update_mask, upc_col].astype("string").head(5)
        audit_df = pd.DataFrame({
            raw_upc_pl_brand_col: raw_brand_series[update_mask].head(5).tolist(),
            f"{upc_col}_before": upc_before.head(5).tolist(),
            key_col: key_values.head(5).tolist(),
            f"{upc_col}_after": upc_after.tolist(),
        })
        print(f"\n{INDENT}Examples (up to 5 rows):")
        _print_df(audit_df, indent=INDENT)

    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 5: Private Label Retailer Re-tagging
# ═══════════════════════════════════════════════════════════════════════════

def apply_private_label_rules(
    df: pd.DataFrame,
    pl_config: dict,
    raw_parent_col: str = "RAW_PARENT",
    tool_brand_col: str = "TOOL_BRAND",
    show_examples: bool = True,
    valid_model_suffixes: set = None,
    pl_base_name: str = "",
) -> pd.DataFrame:
    """
    Tag private label rows with retailer-specific labels (e.g. PRIVATE LABEL RESTRICTED).

    Applies rules to all TOOL_BRAND variants (base + model suffixes) by default.

    When *pl_base_name* is set (e.g. "SUBBRAND"), rules are applied to ALL
    ``TOOL_{pl_base_name}*`` columns directly — bypassing the model-suffix
    whitelist.  Use this for multi-model projects where the PL attribute is
    not TOOL_BRAND but e.g. TOOL_SUBBRAND_DRUG / TOOL_SUBBRAND_MULO / etc.,
    including combined-model columns (e.g. TOOL_SUBBRAND_WT) that have no
    matching subdirectory and would otherwise be missed.

    pl_config example::

        {
            "walmart": {"enabled": True, "label": "PRIVATE LABEL RESTRICTED"},
            "cvs":     {"enabled": True, "label": "PRIVATE LABEL EXCLUDED"},
            "heb":     {"enabled": False, "label": "PRIVATE LABEL RESTRICTED"},
        }
    """
    _print_step_header("5", "Private Label Retailer Re-tagging")

    col_upper_map = {c.upper(): c for c in df.columns}
    if raw_parent_col.upper() not in col_upper_map:
        print(f"{INDENT}Missing column: [{raw_parent_col}]. No changes made.")
        return df
    raw_parent_col = col_upper_map[raw_parent_col.upper()]

    # Determine which TOOL_* columns to target
    if pl_base_name:
        # Explicit base name override: scan ALL TOOL_{base} and TOOL_{base}_* columns,
        # regardless of valid_model_suffixes. This catches combined-model columns
        # (e.g. TOOL_SUBBRAND_WT) that have no matching subdirectory.
        base_upper = pl_base_name.strip().upper()
        tool_brand_variants = []
        for col_upper, col_actual in col_upper_map.items():
            if col_upper == f"TOOL_{base_upper}":
                tool_brand_variants.append((col_actual, ""))
            elif col_upper.startswith(f"TOOL_{base_upper}_"):
                suffix = col_upper[len(f"TOOL_{base_upper}_"):]
                if suffix and f"{base_upper}_{suffix}" in col_upper_map:
                    tool_brand_variants.append((col_actual, suffix))
        tool_brand_variants.sort(key=lambda x: (x[1], x[0]))
        if not tool_brand_variants:
            print(f"{INDENT}No TOOL_{base_upper}* columns found. No changes made.")
            return df
        print(f"{INDENT}PL column override: targeting TOOL_{base_upper}* columns")
    else:
        # Default: auto-detect all TOOL_*/base pairs (respects valid_model_suffixes)
        all_pairs = _find_all_tool_base_pairs(df, valid_suffixes=valid_model_suffixes)
        tool_brand_variants = [(tool_col, suffix) for _, tool_col, suffix in all_pairs]
        if not tool_brand_variants:
            print(f"{INDENT}No TOOL_*/paired columns found. No changes made.")
            return df

    tool_col_names = sorted({tc for tc, _ in tool_brand_variants})
    model_suffixes = sorted({suffix for _, suffix in tool_brand_variants if suffix})
    if model_suffixes:
        print(f"{INDENT}Detected TOOL_* columns: {', '.join(tool_col_names)} (model variants: {', '.join(model_suffixes)})")
        print(f"{INDENT}Applying private label rules to all TOOL_* columns.")
    else:
        print(f"{INDENT}Applying private label rules to column(s): {', '.join(tool_col_names)}")

    raw_parent_series = df[raw_parent_col].astype("string")

    # Retailer name → regex pattern for matching in RAW_PARENT
    heb_pattern = r"h[\s\-]?e[\s\-]?b"
    retailer_patterns = {
        "walmart": r"\bwalmart\b",
        "heb": rf"\b{heb_pattern}\b",
        "cvs": r"\bcvs\b",
    }

    # Apply rules to each TOOL_BRAND variant
    for tb_column, model_suffix in tool_brand_variants:
        tool_brand_series = df[tb_column].astype("string")
        is_private_label = tool_brand_series.str.contains("private label", case=False, na=False)
        column_label = tb_column if not model_suffix else f"{tb_column} (model={model_suffix})"
        print(f"\n{INDENT}Processing: {column_label}")

        for retailer_name, retailer_config in pl_config.items():
            if not retailer_config.get("enabled", False):
                continue
            pattern = retailer_patterns.get(retailer_name)
            if not pattern:
                continue

            match_mask = is_private_label & raw_parent_series.str.contains(
                pattern, case=False, regex=True, na=False
            )
            matched_count = int(match_mask.sum())
            if matched_count:
                label = retailer_config.get("label", f"PRIVATE LABEL {retailer_name.upper()}")
                df.loc[match_mask, tb_column] = label
            print(f"{INDENT}  · {retailer_name.title()} rule applied to {matched_count} row(s).")

    # Show examples from the base TOOL_BRAND column
    if show_examples:
        combined_pattern = "|".join(retailer_patterns.values())
        examples_mask = raw_parent_series.str.contains(
            combined_pattern, case=False, regex=True, na=False
        )
        base_tb_col = tool_brand_col if tool_brand_col in df.columns else tool_brand_variants[0][0]
        example_columns = [raw_parent_col, base_tb_col]
        example_rows = df.loc[examples_mask, example_columns].head(5)
        print(f"\n{INDENT}Examples after tagging:")
        _print_df(example_rows, indent=INDENT)

    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 7: UPC10 Normalization
# ═══════════════════════════════════════════════════════════════════════════

def normalize_upc10(
    df: pd.DataFrame,
    upc_col: str = "UPC10",
    attr_col: str = "UPC10_ATTR",
    create_attr_if_missing: bool = True,
) -> pd.DataFrame:
    """Left-pad UPC10 to 10 characters and mirror the result into UPC10_ATTR."""
    _print_step_header("7", "UPC10 Normalization (Leading Zero Padding -> ensure 10 char length)")

    col_upper_map = {c.upper(): c for c in df.columns}

    if upc_col.upper() not in col_upper_map:
        print(f"{INDENT}Column '{upc_col}' not found. No changes made.")
        return df
    upc_col = col_upper_map[upc_col.upper()]

    attr_col_resolved = col_upper_map.get(attr_col.upper())

    # Normalize: strip whitespace and left-pad to 10 characters
    upc_before = df[upc_col].astype("string").str.strip()
    upc_after = upc_before.str.zfill(10)
    df[upc_col] = upc_after

    values_changed = int(
        (upc_before.ne(upc_after) & ~(upc_before.isna() & upc_after.isna())).sum()
    )

    # Mirror normalized values into UPC10_ATTR
    if attr_col_resolved:
        df[attr_col_resolved] = upc_after
        created_note = ""
        attr_col = attr_col_resolved
    elif create_attr_if_missing:
        df[attr_col] = upc_after
        created_note = " (column created)"
    else:
        print(f"{INDENT}Leading zero normalization applied to '{upc_col}' ({values_changed} value(s) updated).")
        print(f"{INDENT}Note: '{attr_col}' not present; set `create_attr_if_missing=True` to mirror values.")
        return df

    print(f"{INDENT}✓ Leading zero normalization applied to '{upc_col}' ({values_changed} value(s) updated).")
    print(f"{INDENT}✓ Mirrored normalized values into '{attr_col}'{created_note}.")
    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 10.6: Legacy Brand Override Cleanup
# ═══════════════════════════════════════════════════════════════════════════

def strip_legacy_brand_overrides(
    df: pd.DataFrame,
    config: Dict[str, Any],
    show_examples: bool = True,
    max_examples: int = 2,
    valid_model_suffixes: set = None,
) -> pd.DataFrame:
    """
    Reset TOOL_BRAND to BRAND for rows carrying a configured override value
    that belongs to a different manufacturer.

    For each rule, collects the specific TOOL_BRAND override values (e.g.
    "AO PEPSICO BRAND") and the manufacturers that own them (e.g.
    "PEPSICO INC").  Any row whose TOOL_BRAND matches one of those values
    but whose manufacturer does NOT match the owning rule gets reset to
    BRAND — removing override artifacts that don't belong to that parent.

    This is the "blank slate" counterpart to step 11 (apply_brand_overrides),
    mirroring the pattern of steps 10.5 / 12 for RESTRICTED.
    """
    _print_step_header("10.6", "Legacy Brand Override Cleanup")

    if not config or not config.get("enable", True):
        print(f"{INDENT}Brand overrides disabled via config. No cleanup needed.")
        return df

    raw_manufacturer_col = config.get("raw_manufacturer_col", "RAW_MANUFACTURER")
    brand_base = config.get("brand_col", "BRAND")
    tool_base = config.get("tool_brand_col", "TOOL_BRAND")

    col_upper_map = {c.upper(): c for c in df.columns}

    # Resolve manufacturer column (case-insensitive)
    manufacturer_col_resolved: Optional[str] = None
    if raw_manufacturer_col in df.columns:
        manufacturer_col_resolved = raw_manufacturer_col
    else:
        candidate = col_upper_map.get(raw_manufacturer_col.upper())
        if candidate:
            manufacturer_col_resolved = candidate

    if manufacturer_col_resolved is None:
        print(f"{INDENT}Missing required column: [{raw_manufacturer_col}]. No cleanup made.")
        return df

    # Find all BRAND / TOOL_BRAND column pairs (including multi-model)
    brand_tool_pairs = _find_brand_tool_pairs(df, brand_base, tool_base, valid_suffixes=valid_model_suffixes)
    if not brand_tool_pairs:
        print(f"{INDENT}Missing BRAND / TOOL_BRAND columns. No cleanup made.")
        return df

    # Build per-rule mapping: override TOOL_BRAND value → set of manufacturers
    # that own it.  Only these specific values get stripped, and only from rows
    # whose manufacturer does NOT match the rule's configured manufacturers.
    rules: List[Dict[str, Any]] = config.get("rules", [])

    # override_value (casefold) → set of manufacturer keys (casefold)
    override_ownership: Dict[str, set] = {}
    for rule in rules:
        manufacturer_set = {
            str(m).strip().casefold()
            for m in rule.get("manufacturers", [])
            if str(m).strip()
        }
        if not manufacturer_set:
            continue
        for override_val in rule.get("brand_overrides", {}).values():
            key = str(override_val).strip().casefold()
            if key:
                override_ownership.setdefault(key, set()).update(manufacturer_set)

    if not override_ownership:
        print(f"{INDENT}✓ No override TOOL_BRAND values found in rules. No cleanup needed.")
        return df

    # Normalize manufacturer column for case-insensitive comparison
    manufacturer_normalized = df[manufacturer_col_resolved].astype("string").str.strip().str.casefold()

    total_reset = 0

    for brand_col, tool_col, model_suffix in brand_tool_pairs:
        pair_label = (
            f"{brand_col} / {tool_col}"
            if not model_suffix
            else f"{brand_col} / {tool_col} (model={model_suffix})"
        )

        tool_normalized = df[tool_col].astype("string").str.strip().str.casefold().fillna("")

        # For each configured override value, strip it from rows whose
        # manufacturer does not belong to the rule that owns it.
        pair_reset_mask = pd.Series(False, index=df.index)

        for ov_value, owning_manufacturers in override_ownership.items():
            has_override_value = tool_normalized == ov_value
            wrong_manufacturer = ~manufacturer_normalized.isin(owning_manufacturers)
            pair_reset_mask = pair_reset_mask | (has_override_value & wrong_manufacturer)

        reset_count = int(pair_reset_mask.sum())
        if reset_count:
            before_values = df[tool_col].astype("string").str.strip().loc[pair_reset_mask].copy()
            brand_series = df[brand_col].astype("string").str.strip().fillna("")

            # Reset TOOL_BRAND = BRAND
            df.loc[pair_reset_mask, tool_col] = df.loc[pair_reset_mask, brand_col]

            total_reset += reset_count
            print(f"\n{INDENT}  {pair_label}: Reset {reset_count} row(s)")

            if show_examples:
                examples_df = (
                    pd.DataFrame({
                        manufacturer_col_resolved: df.loc[pair_reset_mask, manufacturer_col_resolved],
                        f"{brand_col} (BRAND)": brand_series.loc[pair_reset_mask],
                        f"{tool_col} before": before_values,
                        f"{tool_col} after": df.loc[pair_reset_mask, tool_col],
                    })
                    .drop_duplicates()
                    .head(max_examples)
                )
                _print_df(examples_df, indent=INDENT + "    ")

    if total_reset > 0:
        print(f"{INDENT}✓ Reset tool column to base column for {total_reset} row(s) with non-matching manufacturers.")
    else:
        print(f"{INDENT}✓ No stale override values found to clean.")

    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 11: Client Brand Mapping Overrides
# ═══════════════════════════════════════════════════════════════════════════

def apply_brand_overrides(
    df: pd.DataFrame,
    config: Dict[str, Any],
    show_examples: bool = True,
    max_examples: int = 5,
    valid_model_suffixes: set = None,
) -> pd.DataFrame:
    """
    Replace TOOL_BRAND values based on manufacturer + brand override rules.

    Supports multi-model variants (e.g. BRAND_MULO / TOOL_BRAND_MULO).
    Matching is case-insensitive and whitespace-tolerant.
    """
    _print_step_header("11", "Client Brand Mapping Overrides")

    if not config or not config.get("enable", True):
        print(f"{INDENT}Brand overrides disabled via config. No changes made.")
        return df

    raw_manufacturer_col = config.get("raw_manufacturer_col", "RAW_MANUFACTURER")
    brand_base = config.get("brand_col", "BRAND")
    tool_base = config.get("tool_brand_col", "TOOL_BRAND")

    col_upper_map = {c.upper(): c for c in df.columns}

    # Resolve manufacturer column (case-insensitive)
    manufacturer_col_resolved: Optional[str] = None
    if raw_manufacturer_col in df.columns:
        manufacturer_col_resolved = raw_manufacturer_col
    else:
        candidate = col_upper_map.get(raw_manufacturer_col.upper())
        if candidate:
            manufacturer_col_resolved = candidate

    if manufacturer_col_resolved is None:
        print(f"{INDENT}Missing required column: [{raw_manufacturer_col}]. No changes made.")
        return df

    # Find all BRAND / TOOL_BRAND column pairs
    brand_tool_pairs = _find_brand_tool_pairs(df, brand_base, tool_base, valid_suffixes=valid_model_suffixes)
    if not brand_tool_pairs:
        print(
            f"{INDENT}Missing BRAND / TOOL_BRAND columns for overrides. "
            f"Expected '{brand_base}' / '{tool_base}' or suffixed variants. No changes made."
        )
        return df

    model_suffixes = sorted({suffix for _, _, suffix in brand_tool_pairs if suffix})
    if model_suffixes:
        print(f"{INDENT}Detected model variants: {', '.join(model_suffixes)}")
        print(f"{INDENT}Brand override rules will be applied to all variants.")
    else:
        print(f"{INDENT}Applying overrides to: {brand_tool_pairs[0][0]} / {brand_tool_pairs[0][1]}")

    # Normalization helpers for case-insensitive matching
    def _normalize_series(series: pd.Series) -> pd.Series:
        return series.astype("string").str.strip().str.casefold()

    def _normalize_value(value: Any) -> str:
        return str(value).strip().casefold()

    manufacturer_normalized = _normalize_series(df[manufacturer_col_resolved])

    rules: List[Dict[str, Any]] = config.get("rules", [])
    if not rules:
        print(f"{INDENT}No brand override rules provided. No changes made.")
        return df

    total_updates = 0

    for brand_col, tool_brand_col, model_suffix in brand_tool_pairs:
        brand_normalized = _normalize_series(df[brand_col])
        tool_brand_normalized = _normalize_series(df[tool_brand_col])
        pair_label = (
            f"{brand_col} / {tool_brand_col}"
            if not model_suffix
            else f"{brand_col} / {tool_brand_col} (model={model_suffix})"
        )
        print(f"\n{INDENT}Processing: {pair_label}")

        for rule_index, rule in enumerate(rules, start=1):
            manufacturers = rule.get("manufacturers", [])
            brand_overrides = rule.get("brand_overrides", {})
            only_if_equal = bool(rule.get("only_if_tool_brand_equals_brand", False))

            if not manufacturers or not brand_overrides:
                print(f"{INDENT}  Rule {rule_index}: invalid (needs 'manufacturers' and 'brand_overrides'). Skipped.")
                continue

            manufacturer_set = {_normalize_value(m) for m in manufacturers}
            override_map = {_normalize_value(k): v for k, v in brand_overrides.items()}

            for brand_key, new_tool_brand in override_map.items():
                match_mask = manufacturer_normalized.isin(manufacturer_set) & brand_normalized.eq(brand_key)
                if only_if_equal:
                    match_mask = match_mask & tool_brand_normalized.eq(brand_normalized)

                matched_count = int(match_mask.sum())
                if not matched_count:
                    continue

                df.loc[match_mask, tool_brand_col] = new_tool_brand
                tool_brand_normalized.loc[match_mask] = _normalize_value(new_tool_brand)
                total_updates += matched_count
                print(f"\n{INDENT}  '{brand_key}' → '{new_tool_brand}' applied to {matched_count} row(s)")

                if show_examples:
                    display_columns = [manufacturer_col_resolved, brand_col, tool_brand_col]
                    example_rows = df.loc[match_mask, display_columns].head(max_examples)
                    _print_df(example_rows, indent=INDENT + "    ")

    if total_updates == 0:
        print(f"\n{INDENT}No rows matched any brand override rule.")
    else:
        print(f"\n{INDENT}Total brand override updates: {total_updates} row(s).")

    return df


def strip_legacy_restricted_suffix(
    df: pd.DataFrame,
    show_examples: bool = True,
    max_examples: int = 3,
    valid_model_suffixes: set = None,
) -> pd.DataFrame:
    """
    Remove 'RESTRICTED' suffix from TOOL_BRAND columns for non-private-label values.

    Cleans up legacy data before the restricted rules are re-applied in step 12.
    Values containing 'PRIVATE LABEL' are preserved (e.g. 'PRIVATE LABEL RESTRICTED').
    """
    _print_step_header("10.5", "Legacy RESTRICTED Suffix Cleanup")

    # Find all TOOL_*/base paired columns (any base name, e.g. TOOL_BRAND, TOOL_SUBBRAND)
    all_pairs = _find_all_tool_base_pairs(df, valid_suffixes=valid_model_suffixes)
    tool_brand_variants = [(tool_col, suffix) for _, tool_col, suffix in all_pairs]
    if not tool_brand_variants:
        print(f"{INDENT}No TOOL_*/paired columns found. No changes made.")
        return df

    model_suffixes = [suffix for _, suffix in tool_brand_variants if suffix]
    if model_suffixes:
        print(f"{INDENT}Detected TOOL_* variants: {', '.join(sorted({tc for tc, _ in tool_brand_variants}))}")

    # Regex strings (more pandas-compatible than compiled patterns)
    trailing_restricted_pat = r"[\s_\-]*restricted\s*$"
    private_label_pat = r"private\s+label"

    total_cleaned = 0

    for tool_column, model_suffix in tool_brand_variants:
        column_label = tool_column if not model_suffix else f"{tool_column} (model={model_suffix})"
        tool_brand_series = df[tool_column].fillna("").astype("string").str.strip()

        # Find rows with RESTRICTED suffix but NOT private label values
        has_restricted = tool_brand_series.str.contains("restricted", case=False, regex=True, na=False)
        is_private_label = tool_brand_series.str.contains(private_label_pat, case=False, regex=True, na=False)
        cleanup_mask = has_restricted & ~is_private_label

        cleaned_count = int(cleanup_mask.sum())
        if cleaned_count:
            cleaned_values = (
                tool_brand_series.loc[cleanup_mask]
                .str.replace(trailing_restricted_pat, "", case=False, regex=True)
                .str.strip()
            )

            df.loc[cleanup_mask, tool_column] = cleaned_values
            total_cleaned += cleaned_count

            print(f"{INDENT}  · {column_label}: Cleaned {cleaned_count} row(s)")

            if show_examples:
                # Build distinct before→after example pairs (avoid printing duplicates)
                examples_df = (
                    pd.DataFrame({
                        "before": tool_brand_series.loc[cleanup_mask],
                        "after": cleaned_values,
                    })
                    .drop_duplicates()
                    .head(max_examples)
                )

                examples = ", ".join(
                    f"'{b}' → '{a}'" for b, a in zip(examples_df["before"], examples_df["after"])
                )
                print(f"{INDENT}    Examples: {examples}")

        else:
            print(f"\n{INDENT}  · {column_label}: No legacy RESTRICTED values to clean")

    print(f"{INDENT} This is a preliminary processing step to RAW_MULTI_RETAILER_RESTRICTED transformations")

    if total_cleaned > 0:
        print(f"{INDENT}✓ Cleaned RESTRICTED suffix from {total_cleaned} non-private label row(s).")
    else:
        print(f"{INDENT}✓ No legacy RESTRICTED suffixes found to clean.")

    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 12: Restricted Retailer Tagging
# ═══════════════════════════════════════════════════════════════════════════

def raw_multi_restricted_overrides(
    df: pd.DataFrame,
    show_examples: bool = True,
    max_examples: int = 5,
    valid_model_suffixes: set = None,
) -> pd.DataFrame:
    """
    Canonicalize TOOL_BRAND with '_RESTRICTED' suffix when the
    RAW_*_MULTI_RETAILER_RESTRICTED column signals restricted status.

    Also pre-cleans rows where the RAW column is empty but TOOL_BRAND
    has a stale RESTRICTED suffix (legacy data cleanup).
    """
    _print_step_header("12", "Restricted Retailer Tagging")

    # Locate the RAW multi-restricted source column
    col_upper_map = {c.upper(): c for c in df.columns}
    raw_multi_col: Optional[str] = None
    for candidate_name in ("RAW_US_MULTI_RETAILER_RESTRICTED", "RAW_MULTI_RETAILER_RESTRICTED"):
        if candidate_name.upper() in col_upper_map:
            raw_multi_col = col_upper_map[candidate_name.upper()]
            break

    # Find all base/TOOL_* paired columns (any base name, e.g. BRAND, SUBBRAND)
    brand_tool_pairs = _find_all_tool_base_pairs(df, valid_suffixes=valid_model_suffixes)

    if raw_multi_col is None or not brand_tool_pairs:
        missing = []
        if raw_multi_col is None:
            missing.append("RAW_MULTI_RETAILER_RESTRICTED")
        if not brand_tool_pairs:
            missing.append("base / TOOL_* column pairs")
        print(f"{INDENT}Missing required columns: {missing}. No changes made.")
        return df

    model_suffixes = sorted({suffix for _, _, suffix in brand_tool_pairs if suffix})
    tool_col_names = sorted({tool_col for _, tool_col, _ in brand_tool_pairs})
    if model_suffixes:
        print(f"{INDENT}Using multi-restricted column: {raw_multi_col}")
        print(f"{INDENT}Detected TOOL_* model variants: {', '.join(tool_col_names)}")
        print(f"{INDENT}Applying RESTRICTED suffix rules to all variants.")
    else:
        print(f"{INDENT}Using multi-restricted column: {raw_multi_col}")
        print(f"{INDENT}Applying RESTRICTED suffix rules to: {brand_tool_pairs[0][0]} / {brand_tool_pairs[0][1]}")

    # Pre-compute: raw multi column values and "effectively empty" mask
    raw_multi_series = df[raw_multi_col].astype("string")
    raw_multi_cleaned = raw_multi_series.fillna("").astype("string").str.strip()
    is_raw_empty = raw_multi_cleaned.eq("") | raw_multi_cleaned.eq("0")

    # Regex for stripping trailing RESTRICTED patterns
    trailing_restricted = r"(?i)(?:[\s_\-]*restricted\s*)+$"
    private_label_pattern = r"private\s+label"

    def _strip_restricted_suffix(series: pd.Series) -> pd.Series:
        """Vectorized removal of trailing RESTRICTED patterns from a Series."""
        result = series.fillna("").astype(str).str.strip()
        for _ in range(2):  # Two passes to handle double-suffixed values
            result = result.str.replace(trailing_restricted, "", regex=True).str.rstrip(" _-")
        return result

    # --- Pre-cleanup: remove stale RESTRICTED where RAW column is empty ----
    total_precleaned = 0
    for _, tool_column, _ in brand_tool_pairs:
        tool_series = df[tool_column].astype("string")
        stale_mask = (
            is_raw_empty
            & tool_series.str.contains("restricted", case=False, na=False)
            & ~tool_series.str.contains(private_label_pattern, case=False, regex=True, na=False)
        )
        if stale_mask.any():
            stale_count = int(stale_mask.sum())
            total_precleaned += stale_count
            df.loc[stale_mask, tool_column] = _strip_restricted_suffix(df.loc[stale_mask, tool_column])

    if total_precleaned > 0:
        print(f"\n{INDENT}Pre-cleanup: Removed 'RESTRICTED' suffix from {total_precleaned} row(s)")
        print(f"{INDENT}(where '{raw_multi_col}' is blank but TOOL_BRAND had 'RESTRICTED')")

    # --- Main pass: apply RESTRICTED suffix where RAW_MULTI flags it -------
    has_restricted_flag = raw_multi_series.str.contains("restricted", case=False, na=False)

    total_applied = 0
    total_candidates = int(has_restricted_flag.sum())
    total_empty_base = 0
    total_already_correct = 0
    total_private_label = 0

    for brand_col, tool_col, model_suffix in brand_tool_pairs:
        tool_brand_series = df[tool_col].astype("string")
        brand_series = df[brand_col].astype("string")
        pair_label = (
            f"{brand_col} / {tool_col}"
            if not model_suffix
            else f"{brand_col} / {tool_col} (model={model_suffix})"
        )
        print(f"\n{INDENT}Processing: {pair_label}")

        # Base value preference: TOOL_BRAND if non-empty, else BRAND
        tool_brand_not_empty = tool_brand_series.str.strip().ne("").fillna(False)
        base_value = tool_brand_series.where(tool_brand_not_empty, brand_series).fillna("").astype("string").str.strip()

        # Strip existing RESTRICTED suffix to get the clean base
        base_stripped = base_value.copy()
        eligible_indices = df.index[has_restricted_flag]
        base_stripped.loc[eligible_indices] = _strip_restricted_suffix(base_value.loc[eligible_indices])
        has_usable_base = base_stripped.str.len().gt(0)

        # Build the target restricted value: "<base> RESTRICTED"
        canonical_value = (base_stripped + " RESTRICTED").astype("string")

        # Only update where: flagged, has a base, not already correct, not private label
        current_normalized = tool_brand_series.str.strip().str.casefold()
        canonical_normalized = canonical_value.str.strip().str.casefold()
        is_private_label = tool_brand_series.str.contains(private_label_pattern, case=False, regex=True, na=False)

        update_mask = has_restricted_flag & has_usable_base & current_normalized.ne(canonical_normalized) & ~is_private_label

        rows_applied = int(update_mask.sum())
        if rows_applied:
            df.loc[update_mask, tool_col] = canonical_value[update_mask]

        total_applied += rows_applied
        total_empty_base += int((has_restricted_flag & ~has_usable_base).sum())
        total_private_label += int((has_restricted_flag & is_private_label).sum())
        total_already_correct += int(
            (has_restricted_flag & has_usable_base & ~is_private_label & current_normalized.eq(canonical_normalized)).sum()
        )

        # Post-cleanup: remove RESTRICTED where RAW is empty (catch edge cases)
        tool_brand_now = df[tool_col].astype("string")
        post_cleanup_mask = (
            tool_brand_now.str.contains("restricted", case=False, na=False)
            & ~tool_brand_now.str.contains(private_label_pattern, case=False, regex=True, na=False)
            & is_raw_empty
        )
        if post_cleanup_mask.any():
            df.loc[post_cleanup_mask, tool_col] = _strip_restricted_suffix(df.loc[post_cleanup_mask, tool_col])

        if rows_applied and show_examples:
            display_columns = [brand_col, tool_col, raw_multi_col]
            example_rows = df.loc[update_mask, display_columns].head(max_examples)
            _print_df(example_rows, indent=INDENT + "  ")

    # --- Summary -----------------------------------------------------------
    print(
        f"\n{INDENT}Applied 'RESTRICTED' suffix to {total_applied} row(s) "
        f"where '{raw_multi_col}' contains 'RESTRICTED'."
    )

    skipped = total_candidates - total_applied
    if skipped:
        skip_parts = []
        if total_private_label:
            skip_parts.append(f"private label (handled in earlier steps): {total_private_label}")
        if total_already_correct:
            skip_parts.append(f"already correct: {total_already_correct}")
        if total_empty_base:
            skip_parts.append(f"no usable base value: {total_empty_base}")
        print(f"{INDENT}Skipped {skipped} row(s) — {', '.join(skip_parts)}.")

    if total_applied == 0 and skipped == 0:
        print(f"{INDENT}No rows flagged as RESTRICTED in '{raw_multi_col}'.")

    return df
