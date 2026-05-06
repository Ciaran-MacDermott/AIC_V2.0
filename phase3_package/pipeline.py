"""
Pipeline orchestrator for AIC Phase 2 + Phase 3.

Runs the full processing pipeline in sequence:
  Phase 2  – Attribute assembly from workbook sheets (aic_phase2).
  Phase 3  – Quality checks, transformations, SKU collapse, and
             category splitting (quality, transforms, sku_collapse).

Called by ``PipelineWorker`` in the GUI; all ``print()`` output is
captured and streamed to the GUI log widget via a stdout proxy.

Returns
-------
(collapsed_df, duplicate_dimkeys_df)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

import pandas as pd
pd.set_option('display.max_colwidth', None)

# --- Phase 2 imports (attribute assembly + MDM QC) -------------------------
from phase3_package.aic_phase2 import aic_code, run_tool_vs_mdm_qc

# --- Phase 3 transforms (private label, brand overrides, restricted) -------
from phase3_package.transforms import (
    overwrite_upc10_for_private_label,
    apply_private_label_rules,
    normalize_upc10,
    strip_legacy_brand_overrides,
    apply_brand_overrides,
    strip_legacy_restricted_suffix,
    raw_multi_restricted_overrides,
    _print_step_header,
)

# --- Phase 3 quality checks -----------------------------------------------
from phase3_package.quality import (
    update_req_check,
    verify_ao_cat_def,
    demand_group_check,
    check_identifier_numeric_format,
    flag_invalid_headers,
    check_special_chars,
    check_duplicate_dimkeys,
    split_by_raw_assortment_category,
    check_brand_tool_brand_mismatch,
    check_null_modeling_reporting_cols,
)

# --- SKU collapse ----------------------------------------------------------
from phase3_package.sku_collapse import prepare_and_collapse


# ═══════════════════════════════════════════════════════════════════════════
# Formatting Constants
# ═══════════════════════════════════════════════════════════════════════════
MAJOR_SEP = "=" * 70
MINOR_SEP = "-" * 60
INDENT = "   "


# ═══════════════════════════════════════════════════════════════════════════
# Brand-pair resolution (Attributes.txt Brand_Attribute=Y)
# ═══════════════════════════════════════════════════════════════════════════
# Each Attributes.txt declares its model's brand attribute via a row where
# Brand_Attribute=Y; the Attribute_Name on that row is the literal TOOL_*
# column name (e.g. TOOL_BRAND_FOOD, TOOL_BRAND_MULO, TOOL_SUB_BRAND_DRUG).
# Stripping the TOOL_ prefix yields the BRAND-side column.  This means PL
# retagging and brand-override rules don't need a user-supplied
# brand_col / tool_brand_col / pl_base_name — the data is self-describing.

def _log_brand_attribute_row(attrs_df: "pd.DataFrame", *, source: str) -> None:
    """Print which Attribute_Name the brand-attribute flag points at."""
    if "Brand_Attribute" not in attrs_df.columns or "Attribute_Name" not in attrs_df.columns:
        print(f"{INDENT}{source}: no Brand_Attribute column — falling back to TOOL_*/base auto-discovery downstream")
        return
    flagged = attrs_df[
        attrs_df["Brand_Attribute"].astype(str).str.strip().str.upper() == "Y"
    ]
    names = [str(n).strip() for n in flagged["Attribute_Name"].dropna() if str(n).strip()]
    if not names:
        print(f"{INDENT}{source}: no Brand_Attribute=Y row found")
        return
    print(f"{INDENT}{source}: brand attribute = {', '.join(names)}")


def _resolve_brand_pairs(
    combined_attributes_df: Optional["pd.DataFrame"],
    df_columns: List[str],
) -> List[tuple]:
    """
    Build the list of (brand_col, tool_brand_col) pairs from Brand_Attribute=Y
    rows.  Pairs whose columns aren't present in ``df_columns`` are dropped
    with a warning so downstream loops don't KeyError on stale Attributes.txt.
    """
    if combined_attributes_df is None:
        return []
    if "Brand_Attribute" not in combined_attributes_df.columns:
        return []
    if "Attribute_Name" not in combined_attributes_df.columns:
        return []

    flagged = combined_attributes_df[
        combined_attributes_df["Brand_Attribute"].astype(str).str.strip().str.upper() == "Y"
    ]
    col_upper_map = {str(c).upper(): c for c in df_columns}

    pairs: List[tuple] = []
    seen: set = set()
    for raw_name in flagged["Attribute_Name"].dropna():
        tool_name = str(raw_name).strip().upper()
        if not tool_name.startswith("TOOL_"):
            print(f"{INDENT}⚠ Brand_Attribute=Y row has Attribute_Name={raw_name!r}; expected TOOL_-prefixed name — skipping")
            continue
        brand_name = tool_name[len("TOOL_"):]
        if not brand_name:
            continue
        tool_actual = col_upper_map.get(tool_name)
        brand_actual = col_upper_map.get(brand_name)
        if tool_actual is None or brand_actual is None:
            print(f"{INDENT}⚠ Brand_Attribute=Y declares {tool_name} but its column pair isn't in the data — skipping")
            continue
        key = (brand_actual, tool_actual)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


# ═══════════════════════════════════════════════════════════════════════════
# Main Pipeline
# ═══════════════════════════════════════════════════════════════════════════

def run_through_step_12(
    directory_path: str,
    raw_upc_pl_brand_col: str,
    private_label_config: Dict[str, Any],
    brand_override_config: Dict[str, Any],
    is_custom_collapse: bool,
    file_manifest: Dict[str, Any] = None,
    skip_rmrr: bool = False,
) -> tuple:
    """
    Run Steps 1-12 plus mismatch detection (Phase A).

    Returns
    -------
    (df, duplicate_dimkeys_df, mismatch_groups, pipeline_context)
        - df: DataFrame after all transformations through Step 12.
        - duplicate_dimkeys_df: QC DataFrame of duplicate ITEM_DIM_KEYs found.
        - mismatch_groups: List of per-model mismatch dicts (empty list if none).
        - pipeline_context: Dict carrying forward state needed by Phase B.
    """
    input_dir = Path(directory_path)

    # ------------------------------------------------------------------
    # Build file manifest (scan directory ONCE if not pre-provided)
    # ------------------------------------------------------------------
    if file_manifest is None:
        file_manifest = _scan_directory(input_dir)

    # ------------------------------------------------------------------
    # Read and concatenate all ModelInfo files from manifest
    # ------------------------------------------------------------------
    model_info_paths = file_manifest["model_info_paths"]
    model_info_frames: List[pd.DataFrame] = []
    for file_path in model_info_paths:
        try:
            model_info_frame = pd.read_csv(file_path, sep="|")
            model_info_frames.append(model_info_frame)
        except PermissionError:
            raise PermissionError(
                f"\n{'=' * 60}\n"
                f"FILE LOCKED: Cannot access '{Path(file_path).name}'\n"
                f"{'=' * 60}\n"
                f"The file appears to be open in another application.\n\n"
                f"Please close the file and try again.\n"
                f"{'=' * 60}"
            )
        except Exception as exc:
            raise RuntimeError(f"Error reading ModelInfo file at {file_path}: {exc}")

    model_info_df = pd.concat(model_info_frames, ignore_index=True)

    # Log which ModelInfo files were used
    if len(model_info_paths) == 1:
        relative_path = Path(model_info_paths[0]).relative_to(input_dir)
        print(f"{INDENT}Using ModelInfo.txt from: {relative_path}")
    else:
        relative_paths = ", ".join(
            str(Path(p).relative_to(input_dir)) for p in model_info_paths
        )
        print(f"{INDENT}Detected multiple ModelInfo.txt files in subdirectories: {relative_paths}")
        print(f"{INDENT}Combined all ModelInfo files into a single frame for QC checks.")

    # ==================================================================
    # Phase 2: AIC Attribute Assembly
    # ==================================================================
    print(f"\n{MAJOR_SEP}")
    print("PHASE 2 AIC PROCESSING")
    print(MAJOR_SEP)

    # skip_qc=True because QC runs later on fully transformed data
    # Pass the file manifest so aic_code() skips its own directory scans
    df, _, meta_df, combined_attributes_df, combined_attr_values_df, demand_group_fallback = aic_code(
        str(input_dir), skip_qc=True, file_manifest=file_manifest
    )

    # ==================================================================
    # Phase 3: Quality Checks and Transformations (Steps 1-12)
    # ==================================================================
    print(f"\n{MAJOR_SEP}")
    print("PHASE 3 QUALITY CHECKS AND TRANSFORMATIONS")
    print(MAJOR_SEP)

    # --- Derive valid model suffixes from subdirectories ------------------
    tool_sources = file_manifest.get("tool_sources", [])
    subdir_names = [
        s.split(":", 1)[1] for s in tool_sources if s.startswith("subdir:")
    ]

    valid_model_suffixes: Optional[set] = None
    if subdir_names:
        # Discover known column suffixes from any TOOL_X / X paired columns
        # (e.g. TOOL_BRAND/BRAND, TOOL_SUBBRAND/SUBBRAND) — two-pass approach:
        # pass 1 finds unambiguous base pairs, pass 2 finds their suffixed variants.
        col_upper_set = {str(c).upper() for c in df.columns}
        known_suffixes: set = set()
        for col_key in col_upper_set:
            if col_key.startswith("TOOL_"):
                base = col_key[len("TOOL_"):]
                if base and "_" not in base and base in col_upper_set:
                    # Confirmed base pair — scan for X_SUFFIX / TOOL_X_SUFFIX variants
                    base_prefix = f"{base}_"
                    for other_col in col_upper_set:
                        if other_col.startswith(base_prefix):
                            suffix = other_col[len(base_prefix):]
                            if suffix and f"TOOL_{base}_{suffix}" in col_upper_set:
                                known_suffixes.add(suffix)

        # Additional pass: directly validate directory names as column suffixes.
        # Handles projects where only suffixed columns exist (no base TOOL_X/X pair),
        # e.g. SUBBRAND_MULO / TOOL_SUBBRAND_MULO with no base SUBBRAND / TOOL_SUBBRAND.
        for dir_name in subdir_names:
            dir_upper = dir_name.upper()
            if dir_upper not in known_suffixes:
                suffix_tag = f"_{dir_upper}"
                for col_key in col_upper_set:
                    if col_key.startswith("TOOL_") and col_key.endswith(suffix_tag):
                        non_tool = col_key[len("TOOL_"):]
                        if non_tool in col_upper_set:
                            known_suffixes.add(dir_upper)
                            break

        valid_model_suffixes = set()
        print(f"{INDENT}Multi-model subdirectories detected:")

        for dir_name in sorted(subdir_names):
            dir_upper = dir_name.upper()

            # Exact match (e.g. directory called "CONV")
            if dir_upper in known_suffixes:
                valid_model_suffixes.add(dir_upper)
                print(f"{INDENT}  {dir_name} → {dir_upper} (confirmed column suffix _{dir_upper})")
                continue

            # Segment match — check each underscore-delimited part
            segments = dir_upper.split("_")
            matched = [seg for seg in segments if seg in known_suffixes]

            if len(matched) == 1:
                valid_model_suffixes.add(matched[0])
                print(f"{INDENT}  {dir_name} → {matched[0]} (confirmed column suffix _{matched[0]})")
            elif len(matched) > 1:
                # Ambiguous — use last matching segment
                chosen = matched[-1]
                valid_model_suffixes.add(chosen)
                print(f"{INDENT}  ⚠ {dir_name} matches multiple suffixes {matched} — using {chosen}")
            else:
                # No column match — fall back to last segment and warn
                fallback = segments[-1]
                valid_model_suffixes.add(fallback)
                print(
                    f"{INDENT}  ⚠ {dir_name} → {fallback} (no matching column pair with suffix _{fallback} found — "
                    f"expected e.g. BRAND_{fallback} / TOOL_BRAND_{fallback} or similar TOOL_*/base pair)"
                )

        valid_model_suffixes = valid_model_suffixes or None

    if valid_model_suffixes:
        print(f"{INDENT}Active model suffixes: {', '.join(sorted(valid_model_suffixes))}")
        print(f"{INDENT}Only column pairs matching these suffixes will be processed.\n")

    # Step 1: Ensure UPDATE_REQUIRED is set to 1 for all rows
    df = update_req_check(df)

    # Step 2: Align ASSORTMENT_CATEGORY_DEFINITION to ModelInfo
    df = verify_ao_cat_def(df, model_info_df)

    # Step 3: Audit DEMAND_GROUP and interaction columns
    df = demand_group_check(df, demand_group_fallback=demand_group_fallback)

    # Step 4: Overwrite UPC10 with ITEM_DIM_KEY for private label rows
    df = overwrite_upc10_for_private_label(df, raw_upc_pl_brand_col)

    # Step 5: Apply retailer-specific private label tagging.  raw_parent_col
    # is sourced from the brand-override config so the analyst's choice is
    # honored end-to-end (detection + dialog).  Falls back to the function
    # default ("RAW_PARENT") when not configured.
    raw_parent_col = brand_override_config.get("raw_parent_col", "RAW_PARENT") or "RAW_PARENT"

    # Resolve (brand_col, tool_brand_col) pairs from each Attributes.txt's
    # Brand_Attribute=Y row.  Each row's Attribute_Name is the literal TOOL
    # column (e.g. TOOL_BRAND_FOOD); stripping TOOL_ gives the BRAND column.
    # Empty list when Brand_Attribute is absent / all-N — downstream falls
    # back to TOOL_*/base auto-discovery.
    brand_pairs = _resolve_brand_pairs(combined_attributes_df, list(df.columns))
    if brand_pairs:
        pair_summary = ", ".join(f"{b}/{t}" for b, t in brand_pairs)
        print(f"{INDENT}Resolved brand pairs from Attributes.txt: {pair_summary}")

    df = apply_private_label_rules(
        df,
        private_label_config,
        raw_parent_col=raw_parent_col,
        show_examples=True,
        valid_model_suffixes=valid_model_suffixes,
        brand_pairs=brand_pairs or None,
    )

    # Step 6: Check UPC10/SKU/ITEM_DIM_KEY for scientific notation / decimals
    check_identifier_numeric_format(df, cols=("UPC10", "SKU", "ITEM_DIM_KEY"))

    # Step 7: Left-pad UPC10 to 10 characters and mirror to UPC10_ATTR
    df = normalize_upc10(df, upc_col="UPC10")

    # Step 8: Drop unnamed columns, flag non-standard column names
    df = flag_invalid_headers(df)

    # Step 9: Replace '/' with ' OR ' in reporting columns, flag special chars
    df = check_special_chars(df, suffix=None)

    # Step 10: Deduplicate ITEM_DIM_KEYs (keep highest-dollar row)
    df, duplicate_dimkeys_df = check_duplicate_dimkeys(df)

    # Step 10.5: Strip legacy RESTRICTED suffix before brand override cleanup
    df = strip_legacy_restricted_suffix(df, valid_model_suffixes=valid_model_suffixes)

    # Step 10.6: Strip stale brand overrides for non-configured manufacturers
    df = strip_legacy_brand_overrides(
        df,
        brand_override_config,
        valid_model_suffixes=valid_model_suffixes,
        brand_pairs=brand_pairs or None,
    )

    # Step 11: Apply client brand mapping overrides
    df = apply_brand_overrides(
        df,
        brand_override_config,
        valid_model_suffixes=valid_model_suffixes,
        brand_pairs=brand_pairs or None,
    )

    # Step 12: Canonicalize TOOL_BRAND with _RESTRICTED where RAW_MULTI signals it
    # Skipped for non-MULO+ geo groupings where RMRR tagging does not apply
    if not skip_rmrr:
        df = raw_multi_restricted_overrides(df, valid_model_suffixes=valid_model_suffixes)

    # Step 12.5: QC — flag BRAND vs TOOL_BRAND mismatches (potential DB logic issues).
    # raw_parent_col drives both AO grouping and the dialog's PARENT column;
    # raw_manufacturer_col is passed for the legacy fallback path inside the
    # check (kept so configs that haven't migrated still work).
    mismatch_groups = check_brand_tool_brand_mismatch(
        df,
        raw_manufacturer_col=brand_override_config.get("raw_manufacturer_col", ""),
        raw_parent_col=raw_parent_col,
        valid_model_suffixes=valid_model_suffixes,
    )

    # Build pipeline context for Phase B
    pipeline_context = {
        "meta_df": meta_df,
        "combined_attributes_df": combined_attributes_df,
        "combined_attr_values_df": combined_attr_values_df,
        "duplicate_dimkeys_df": duplicate_dimkeys_df,
        "input_dir": str(input_dir),
        "is_custom_collapse": is_custom_collapse,
        "raw_manufacturer_col": brand_override_config.get("raw_manufacturer_col", ""),
        "raw_parent_col": raw_parent_col,
        "valid_model_suffixes": valid_model_suffixes,
    }

    return df, duplicate_dimkeys_df, mismatch_groups, pipeline_context


def run_from_step_14(
    df: pd.DataFrame,
    pipeline_context: Dict[str, Any],
    corrections: Optional[list] = None,
) -> tuple:
    """
    Apply BRAND / TOOL_BRAND corrections (if any), then run Steps 14-17 (Phase B).

    The category split is **not** performed here — the collapsed output is
    written as a single sheet so analysts can review and edit before the
    post-QC stage re-collapses and exports CSVs.

    Parameters
    ----------
    df : DataFrame
        The DataFrame from Phase A (after Step 12).
    pipeline_context : dict
        State carried forward from ``run_through_step_12()``.
    corrections : list of dict, optional
        Each dict has ``type`` ("brand" or "tool_brand") plus the
        original values, new value, and actual column names in df.

    Returns
    -------
    (collapsed_df, duplicate_dimkeys_df)
    """
    meta_df = pipeline_context["meta_df"]
    combined_attributes_df = pipeline_context["combined_attributes_df"]
    combined_attr_values_df = pipeline_context["combined_attr_values_df"]
    duplicate_dimkeys_df = pipeline_context["duplicate_dimkeys_df"]
    input_dir = pipeline_context["input_dir"]
    is_custom_collapse = pipeline_context["is_custom_collapse"]

    # --- Apply user corrections to BRAND / TOOL_BRAND --------------------
    # The dialog renders the parent column under the PARENT header, so the
    # `parent` value on each correction matches raw_parent_col.  Keep
    # raw_manufacturer_col as a fallback for older contexts that haven't
    # been re-emitted by Phase A yet.
    raw_parent_col = pipeline_context.get("raw_parent_col", "")
    raw_manufacturer_col = pipeline_context.get("raw_manufacturer_col", "")
    col_upper_map = {str(c).upper(): c for c in df.columns}
    parent_col_actual = (
        col_upper_map.get(raw_parent_col.upper()) if raw_parent_col else None
    )
    if parent_col_actual is None and raw_manufacturer_col:
        parent_col_actual = col_upper_map.get(raw_manufacturer_col.upper())

    if corrections:
        brand_count = 0
        tool_count = 0
        rows_updated = 0
        corrected_pairs: set = set()
        for fix in corrections:
            brand_col_name = fix.get("brand_col", "BRAND")
            tool_col_name = fix.get("tool_brand_col", "TOOL_BRAND")

            # Row mask: match on original BRAND + TOOL_BRAND values
            mask = (
                (df[brand_col_name].astype(str).str.upper() == fix["brand"].upper())
                & (df[tool_col_name].astype(str).str.upper() == fix["tool_brand_old"].upper())
            )

            # Narrow by parent value when available (AO brand rows).  The
            # column resolved above matches whatever the dialog rendered
            # under the PARENT header, so analyst-supplied parent values
            # land on the right rows even after the parent/manufacturer split.
            parent_val = fix.get("parent", "")
            if parent_val and parent_col_actual:
                mask = mask & (df[parent_col_actual].astype(str).str.upper() == parent_val.upper())

            n_rows = int(mask.sum())
            rows_updated += n_rows
            corrected_pairs.add((fix["brand"], fix["tool_brand_old"]))

            if fix.get("type") == "brand":
                df.loc[mask, brand_col_name] = fix["brand_new"]
                brand_count += 1
            else:
                df.loc[mask, tool_col_name] = fix["tool_brand_new"]
                tool_count += 1

        parts = []
        if brand_count:
            parts.append(f"{brand_count} BRAND")
        if tool_count:
            parts.append(f"{tool_count} TOOL_BRAND")
        n_pairs = len(corrected_pairs)
        print(f"{INDENT}Mismatch review: {' + '.join(parts)} correction(s) manually applied "
              f"— {n_pairs} distinct pair(s), {rows_updated} row(s) updated")


    # Step 14: SKU collapse (top-dollar or custom parent dim-key)
    collapsed_df = prepare_and_collapse(
        df,
        verbose=True,
        is_custom_collapse=is_custom_collapse,
    )

    # Step 15: QC — flag null values in modeling/reporting columns.
    # Run on collapsed_df (the data that will be written to output.xlsx) so that
    # any auto-fills are applied to the output, not to the pre-collapse df which
    # is already discarded.  Previously this ran on df, so auto-fills never
    # reached the output and Post-QC would re-find the same nulls.
    check_null_modeling_reporting_cols(collapsed_df, meta_df=meta_df)

    # Step 16: Tool vs MDM attribute comparison QC (on final transformed data)
    _print_step_header("16", "ATTRIBUTE QC (TOOL VS MDM COMPARISON)")
    valid_model_suffixes = pipeline_context.get("valid_model_suffixes")
    run_tool_vs_mdm_qc(
        input_dir,
        collapsed_df,
        combined_attributes_df=combined_attributes_df,
        combined_attr_values_df=combined_attr_values_df,
        valid_model_suffixes=valid_model_suffixes,
    )

    return collapsed_df, duplicate_dimkeys_df


def run_post_qc(
    excel_path: str,
    is_custom_collapse: bool,
    meta_df: Optional[pd.DataFrame] = None,
) -> tuple:
    """
    Post-QC pipeline: re-validate and re-collapse analyst-edited output.

    Reads the single-sheet Excel file that the analyst has reviewed and
    edited, checks for null values in modeling/reporting columns, re-runs
    SKU collapse (to ensure edits haven't violated collapse rules), and
    splits by category for CSV export.

    Parameters
    ----------
    excel_path : str
        Path to the analyst-edited Excel workbook (single "Cleaned Output" sheet).
    is_custom_collapse : bool
        If True, use analyst-selected parent dim-key for SKU collapse.
    meta_df : DataFrame, optional
        META sheet used for null-check column classification.

    Returns
    -------
    (collapsed_df, category_splits)
        - collapsed_df: Re-collapsed DataFrame.
        - category_splits: Dict mapping category name → DataFrame subset.
    """
    print(f"\n{MAJOR_SEP}")
    print("POST-QC PIPELINE  (Finalize & Export)")
    print(MAJOR_SEP)

    # --- Read the edited Excel file back ---------------------------------
    _print_step_header("Post-Step 1", "Read Edited Output File")
    print(f"{INDENT}Reading: {excel_path}")

    df = pd.read_excel(excel_path, sheet_name="Cleaned Output", engine="openpyxl")
    cleaned_output_rows = len(df)
    print(f"{INDENT}Loaded {cleaned_output_rows} row(s), {len(df.columns)} column(s)")

    # --- Pre-collapse validation -----------------------------------------
    _print_step_header("Post-Step 2", "Pre-Collapse Validation")
    print(f"{INDENT}Running null checks on modeling/reporting columns before SKU re-collapse...")
    check_null_modeling_reporting_cols(df, meta_df=meta_df, show_step_header=False)

    # --- Re-run SKU collapse ---------------------------------------------
    _print_step_header("Post-Step 3", "SKU Re-Collapse")
    print(f"{INDENT}Re-collapsing SKUs to reflect any analyst edits made to the Cleaned Output sheet...")
    collapsed_df = prepare_and_collapse(
        df,
        verbose=True,
        is_custom_collapse=is_custom_collapse,
        show_step_header=False,
    )

    # --- Split by category for CSV export --------------------------------
    _print_step_header("Post-Step 4", "Category Split & Re-Export")
    category_splits = split_by_raw_assortment_category(collapsed_df)

    # --- Multi-category sanity check -------------------------------------
    if len(category_splits) > 1:
        summed_rows = sum(len(cat_df) for cat_df in category_splits.values())
        collapsed_rows = len(collapsed_df)
        match_str = "PASS" if summed_rows == collapsed_rows else "FAIL"
        print(f"\n{INDENT}Sanity check: sum of category sheet rows ({summed_rows}) "
              f"vs Cleaned Output rows ({collapsed_rows}) — {match_str}")
        if summed_rows != collapsed_rows:
            print(f"{INDENT}⚠ Row count mismatch — {abs(summed_rows - collapsed_rows)} row(s) difference. "
                  f"Check for blank or unmapped RAW_ASSORTMENT_CATEGORY values.")

    print(f"\n{INDENT}Post-QC pipeline complete — ready for CSV export.")
    return collapsed_df, category_splits


def main(
    directory_path: str,
    raw_upc_pl_brand_col: str,
    private_label_config: Dict[str, Any],
    brand_override_config: Dict[str, Any],
    is_custom_collapse: bool,
    file_manifest: Dict[str, Any] = None,
):
    """
    Run the full Phase 2 → Phase 3 pipeline and return results.

    Convenience wrapper that calls ``run_through_step_12()`` followed by
    ``run_from_step_14()``.  Preserved for backwards compatibility with
    non-GUI callers.

    Parameters
    ----------
    directory_path : str
        Root folder containing File_For_Mapping_QC.xlsx, ModelInfo.txt,
        and tool/lookup files.
    raw_upc_pl_brand_col : str
        RAW column used for private-label UPC10 overwrite (e.g. "RAW_BRAND").
    private_label_config : dict
        Retailer-specific private label rules (walmart, cvs, heb, etc.).
    brand_override_config : dict
        Manufacturer → brand override mapping rules.
    is_custom_collapse : bool
        If True, use analyst-selected parent dim-key for SKU collapse
        instead of top-dollar row.
    file_manifest : dict, optional
        Pre-scanned directory manifest. When provided, skips all directory
        scanning and redundant file reads. Built by ``_scan_directory()``
        or passed from the GUI layer.

    Returns
    -------
    (collapsed_df, duplicate_dimkeys_df)
        - collapsed_df: Final cleaned DataFrame after all steps.
        - duplicate_dimkeys_df: QC DataFrame of duplicate ITEM_DIM_KEYs found.
    """
    df, dup_df, mismatch_groups, ctx = run_through_step_12(
        directory_path,
        raw_upc_pl_brand_col,
        private_label_config,
        brand_override_config,
        is_custom_collapse,
        file_manifest=file_manifest,
    )
    return run_from_step_14(df, ctx)


# ═══════════════════════════════════════════════════════════════════════════
# Directory Scanner (single scan for entire pipeline)
# ═══════════════════════════════════════════════════════════════════════════

def _scan_directory(input_dir: Path) -> Dict[str, Any]:
    """
    Scan the input directory ONCE and build a manifest of all files
    needed by the pipeline.

    Returns a dict with:
    - ``workbook_path``            : Path to File_For_Mapping_QC.xlsx (or None)
    - ``model_info_paths``         : List of Path to ModelInfo.txt files
    - ``combined_attributes_df``   : Combined Attributes.txt DataFrame (or None)
    - ``combined_attr_values_df``  : Combined AttributeValues.txt DataFrame (or None)
    - ``tool_sources``             : List of source labels (e.g. "root", "subdir:MULO")
    - ``json_config``              : Parsed JSON config dict (or None)
    - ``skipped_files``            : List of skipped file names
    """
    workbook_path: Optional[Path] = None
    model_info_paths: List[Path] = []
    root_model_info: Optional[Path] = None
    all_attributes_dfs: List[pd.DataFrame] = []
    all_attr_values_dfs: List[pd.DataFrame] = []
    tool_sources: List[str] = []
    json_config = None
    skipped_files: List[str] = []
    subdirs: List[Path] = []

    expected_txt_patterns = [
        r"^Attributes\.txt$",
        r"^AttributeValues\.txt$",
        r"(?i)^ModelInfo.*\.txt$",
    ]
    expected_xlsx_pattern = r"(?i).*file_for_mapping_qc.*\.xlsx$"

    def _is_expected_txt(name: str) -> bool:
        return any(re.match(p, name) for p in expected_txt_patterns)

    # --- Scan root directory ------------------------------------------------
    root_has_attributes = False
    root_has_attr_values = False
    root_attributes_df = None
    root_attr_values_df = None

    for entry in input_dir.iterdir():
        if entry.is_dir():
            subdirs.append(entry)
            continue

        if not entry.is_file():
            continue

        name = entry.name
        name_lower = name.lower()

        # Workbook
        if name_lower.endswith(".xlsx") and re.match(expected_xlsx_pattern, name):
            workbook_path = entry
            continue

        # ModelInfo
        if name_lower == "modelinfo.txt":
            root_model_info = entry
            continue

        # Attributes.txt
        if name == "Attributes.txt":
            try:
                root_attributes_df = pd.read_csv(str(entry), delimiter="|")
                root_has_attributes = True
                _log_brand_attribute_row(root_attributes_df, source="root/Attributes.txt")
            except Exception as exc:
                print(f"{INDENT}  ⚠ Skipped {name}: could not parse ({type(exc).__name__})")
                skipped_files.append(name)
            continue

        # AttributeValues.txt
        if name == "AttributeValues.txt":
            try:
                root_attr_values_df = pd.read_csv(str(entry), delimiter="|")
                root_has_attr_values = True
            except Exception as exc:
                print(f"{INDENT}  ⚠ Skipped {name}: could not parse ({type(exc).__name__})")
                skipped_files.append(name)
            continue

        # JSON config
        if name_lower.endswith(".json"):
            try:
                with open(str(entry), "r") as f:
                    json_config = json.load(f)
            except (json.JSONDecodeError, Exception):
                skipped_files.append(name)
            continue

        # Other expected txt files (e.g. other ModelInfo variants)
        if name_lower.endswith(".txt") and _is_expected_txt(name):
            continue

        # Non-essential files
        if not name_lower.endswith(".csv"):
            skipped_files.append(name)

    # Collect root-level tool files
    if root_has_attributes and root_has_attr_values:
        all_attributes_dfs.append(root_attributes_df)
        all_attr_values_dfs.append(root_attr_values_df)
        tool_sources.append("root")

    # Use root ModelInfo if found; otherwise check subdirectories
    if root_model_info:
        model_info_paths = [root_model_info]

    # --- Scan subdirectories -----------------------------------------------
    for subdir in subdirs:
        try:
            sub_entries = {e.name: e for e in subdir.iterdir() if e.is_file()}
        except PermissionError:
            continue

        # ModelInfo in subdirectory (only if not found at root)
        if not root_model_info:
            for sub_name, sub_path in sub_entries.items():
                if sub_name.lower() == "modelinfo.txt":
                    model_info_paths.append(sub_path)

        # Tool files in subdirectory
        attr_entry = sub_entries.get("Attributes.txt")
        attr_val_entry = sub_entries.get("AttributeValues.txt")

        if attr_entry and attr_val_entry:
            try:
                sub_attrs_df = pd.read_csv(str(attr_entry), delimiter="|")
                _log_brand_attribute_row(sub_attrs_df, source=f"{subdir.name}/Attributes.txt")
                all_attributes_dfs.append(sub_attrs_df)
                all_attr_values_dfs.append(pd.read_csv(str(attr_val_entry), delimiter="|"))
                tool_sources.append(f"subdir:{subdir.name}")
            except Exception as exc:
                print(f"{INDENT}Error reading tool files in subdirectory '{subdir.name}': {exc}")

    # --- Validate required files -------------------------------------------
    if not model_info_paths:
        raise FileNotFoundError(
            f"ModelInfo.txt not found in {input_dir} "
            f"or any immediate subdirectory."
        )

    # --- Combine tool DataFrames -------------------------------------------
    combined_attributes_df = None
    combined_attr_values_df = None
    if all_attributes_dfs and all_attr_values_dfs:
        combined_attributes_df = pd.concat(all_attributes_dfs, ignore_index=True).drop_duplicates()
        combined_attr_values_df = pd.concat(all_attr_values_dfs, ignore_index=True).drop_duplicates()

    return {
        "workbook_path": workbook_path,
        "model_info_paths": model_info_paths,
        "combined_attributes_df": combined_attributes_df,
        "combined_attr_values_df": combined_attr_values_df,
        "tool_sources": tool_sources,
        "json_config": json_config,
        "skipped_files": skipped_files,
    }
