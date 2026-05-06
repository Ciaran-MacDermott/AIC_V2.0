"""
Phase 2 AIC (Attribute Item Classification) processing.

Reads the File_For_Mapping_QC workbook along with tool files
(Attributes.txt, AttributeValues.txt) and builds the initial output
DataFrame by:

1. Scanning the directory for input files.
2. Parsing the workbook sheets (FLAT_FILE, META, FINAL template).
3. Running an early ITEM_DIM_KEY dedup to prevent row explosion.
4. Assembling MODELING attributes (left-join to lookup sheets) and
   REPORTING attributes (direct column copy).
5. Building the output DataFrame by mapping to the FINAL template.
6. Optionally running a Tool vs MDM attribute QC comparison.

Functions
---------
aic_code            – Main Phase 2 entry point.
run_tool_vs_mdm_qc – Post-transformation attribute QC comparison.
generate_qc_check   – Core QC logic comparing tool vs MDM attribute values.
"""

import difflib
import json
import os
import re
from typing import Tuple, List, Dict, Any, Optional, Set

import numpy as np
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


def _log_brand_attribute_row(attrs_df: "pd.DataFrame", *, source: str) -> None:
    """Print which Attribute_Name carries Brand_Attribute=Y for a parsed Attributes.txt."""
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


# ═══════════════════════════════════════════════════════════════════════════
# Duplicate Key Validation
# ═══════════════════════════════════════════════════════════════════════════

class AttributeMappingConflictError(Exception):
    """
    Raised when a lookup table contains conflicting mappings for the same
    key combination, which would cause a cross-join explosion during merge.
    """
    pass


def _check_for_duplicate_keys(
    lookup_df: pd.DataFrame,
    key_cols: List[str],
    value_col: str,
    attribute_name: str,
    flat_file_df: pd.DataFrame,
    output_dir: str = None,
) -> None:
    """
    Validate that a lookup table has no duplicate key combinations mapping
    to different values. If conflicts are found, exports a diagnostic
    Excel report and raises ``AttributeMappingConflictError``.

    Parameters
    ----------
    lookup_df : DataFrame
        Lookup table (already filtered to Rank==1).
    key_cols : list of str
        Columns used as merge keys.
    value_col : str
        Column containing the mapped values.
    attribute_name : str
        Name of the attribute (for error messaging).
    flat_file_df : DataFrame
        FLAT_FILE DataFrame (to count affected rows).
    output_dir : str, optional
        Directory for the diagnostic Excel file.
    """
    sheet_name = f"Final_{attribute_name}_lkp"[:31]

    try:
        # Reset indices to avoid alignment issues with boolean masks
        lookup_df = lookup_df.reset_index(drop=True)
        flat_file_reset = flat_file_df.reset_index(drop=True)

        # Group by key columns and check for multiple distinct values
        distinct_value_counts = lookup_df.groupby(key_cols, dropna=False)[value_col].nunique()
        conflicts = distinct_value_counts[distinct_value_counts > 1]

        if conflicts.empty:
            return  # No conflicts — safe to proceed

        # --- Build diagnostic DataFrame of all conflicting rows ------------
        conflict_rows: List[pd.DataFrame] = []
        total_affected_rows = 0

        for key_tuple in conflicts.index:
            key_values = (key_tuple,) if len(key_cols) == 1 else key_tuple

            # Find conflicting rows in the lookup table
            lookup_mask = np.ones(len(lookup_df), dtype=bool)
            for col_name, key_value in zip(key_cols, key_values):
                if pd.isna(key_value):
                    lookup_mask &= lookup_df[col_name].isna().values
                else:
                    lookup_mask &= (lookup_df[col_name] == key_value).values

            conflicting_df = lookup_df.loc[lookup_mask, key_cols + [value_col]].copy()

            # Count how many FLAT_FILE rows would be affected
            flat_mask = np.ones(len(flat_file_reset), dtype=bool)
            for col_name, key_value in zip(key_cols, key_values):
                if col_name in flat_file_reset.columns:
                    if pd.isna(key_value):
                        flat_mask &= flat_file_reset[col_name].isna().values
                    else:
                        flat_mask &= (flat_file_reset[col_name].astype(str) == str(key_value)).values
            affected_count = int(flat_mask.sum())
            total_affected_rows += affected_count

            # Add metadata columns for the report
            conflicting_df["_CONFLICT_GROUP"] = hash(key_values) % 100000
            conflicting_df["_FLAT_FILE_ROWS_AFFECTED"] = affected_count
            conflicting_df["_NUM_CONFLICTING_VALUES"] = len(conflicting_df)
            conflict_rows.append(conflicting_df)

        conflicts_df = pd.concat(conflict_rows, ignore_index=True)

        # Reorder: key columns → value → metadata
        column_order = key_cols + [value_col, "_NUM_CONFLICTING_VALUES", "_FLAT_FILE_ROWS_AFFECTED", "_CONFLICT_GROUP"]
        conflicts_df = conflicts_df[column_order].sort_values(key_cols + [value_col]).reset_index(drop=True)

        # --- Export diagnostic Excel report --------------------------------
        output_path = os.path.join(output_dir or ".", f"CONFLICT_REPORT_{attribute_name}.xlsx")
        try:
            with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
                conflicts_df.to_excel(writer, sheet_name="Conflicting Rows", index=False)

                # Summary sheet: one row per key combination
                summary_df = conflicts_df.groupby(key_cols, dropna=False).agg({
                    value_col: lambda x: " | ".join(sorted(set(str(v) for v in x))),
                    "_FLAT_FILE_ROWS_AFFECTED": "first",
                    "_NUM_CONFLICTING_VALUES": "first",
                }).reset_index()
                summary_df.columns = key_cols + ["CONFLICTING_VALUES", "FLAT_FILE_ROWS_AFFECTED", "NUM_VALUES"]
                summary_df = summary_df.sort_values("FLAT_FILE_ROWS_AFFECTED", ascending=False)
                summary_df.to_excel(writer, sheet_name="Summary", index=False)

                for ws_name in writer.sheets:
                    writer.sheets[ws_name].autofit()

            export_message = f"Diagnostic file exported: {output_path}"
        except Exception as exc:
            export_message = f"(Could not export diagnostic file: {exc})"
            output_path = None

        # Build error message
        avg_values = conflicts_df.groupby(key_cols, dropna=False)[value_col].nunique().mean()
        key_cols_str = ", ".join(key_cols)

        error_lines = [
            "DUPLICATE KEY MAPPING DETECTED",
            "",
            f"Attribute:    {attribute_name}",
            f"Lookup sheet: {sheet_name}",
            f"Key columns:  {key_cols_str}",
            "",
            f"Found {len(conflicts)} key combination(s) with multiple values at Rank=1.",
            f"This would cause row explosion: {total_affected_rows:,} -> ~{int(total_affected_rows * avg_values):,} rows",
            "",
        ]

        if output_path:
            error_lines.extend([
                f"DIAGNOSTIC FILE: {output_path}",
                f"  - 'Conflicting Rows' sheet: all duplicate mappings",
                f"  - 'Summary' sheet: conflicts sorted by impact",
                "",
            ])

        error_lines.extend([
            "TO FIX:",
            f"  1. Open '{sheet_name}' in the input workbook",
            f"  2. Sort by: {key_cols_str}",
            f"  3. For duplicates, delete incorrect rows or set Rank != 1",
            f"  4. Re-run pipeline",
        ])

        raise AttributeMappingConflictError("\n".join(error_lines))

    except AttributeMappingConflictError:
        raise  # Re-raise our own error without wrapping

    except Exception as exc:
        key_cols_str = ", ".join(key_cols)
        raise AttributeMappingConflictError(
            f"LOOKUP TABLE VALIDATION ERROR\n"
            f"\n"
            f"Attribute:    {attribute_name}\n"
            f"Lookup sheet: {sheet_name}\n"
            f"Key columns:  {key_cols_str}\n"
            f"\n"
            f"Error: {type(exc).__name__}: {exc}\n"
            f"\n"
            f"LIKELY CAUSE:\n"
            f"  - Value in wrong column (e.g., attribute value in a key column)\n"
            f"  - Missing or misaligned headers\n"
            f"  - Inconsistent data types\n"
            f"\n"
            f"TO FIX:\n"
            f"  1. Open '{sheet_name}' in the workbook\n"
            f"  2. Check that values are in correct columns\n"
            f"  3. Look for shifted/misaligned rows"
        ) from None


# ═══════════════════════════════════════════════════════════════════════════
# QC Value Formatting Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _is_nonempty(value) -> bool:
    """True if *value* is meaningfully non-empty (handles lists, strings, NaN)."""
    if value is None:
        return False
    if isinstance(value, float) and pd.isna(value):
        return False
    if isinstance(value, list):
        return len([x for x in value if str(x).strip() and str(x).strip().lower() != "nan"]) > 0
    text = str(value).strip()
    return bool(text) and text.lower() != "nan" and text != "[]"


def _fmt_list_cell(value, max_items: int = 12) -> str:
    """Format a list-like value as a compact comma-separated string for log cells."""
    if not _is_nonempty(value):
        return ""
    if isinstance(value, list):
        items = [str(x).strip() for x in value if str(x).strip() and str(x).strip().lower() != "nan"]
        if not items:
            return ""
        if len(items) <= max_items:
            return ", ".join(items)
        return ", ".join(items[:max_items]) + f" … (+{len(items) - max_items} more)"
    return str(value).strip()


def _as_clean_list(value: Any) -> list[str]:
    """
    Normalize a QC cell value into a clean list of strings.

    Handles list objects, NaN, stringified lists like ``"['A', 'B']"``,
    and single strings.
    """
    if not _is_nonempty(value):
        return []

    if isinstance(value, list):
        return [
            str(x).strip() for x in value
            if x is not None and str(x).strip() and str(x).strip().lower() != "nan"
        ]

    text = str(value).strip()
    if text == "[]":
        return []

    # Best-effort parse of simple stringified lists (no eval)
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [
            p.strip().strip("'").strip('"').strip()
            for p in inner.split(",")
            if p.strip().strip("'").strip('"').strip()
        ]

    return [text]


def _fmt_bullets(items: list[str], *, indent: str, max_items: int = 30) -> str:
    """Format a list as sorted bullet points with optional truncation."""
    if not items:
        return f"{indent}- (none)"

    items = sorted(str(x).strip() for x in items if str(x).strip())

    extra = 0
    if len(items) > max_items:
        extra = len(items) - max_items
        items = items[:max_items]

    lines = [f"{indent}- {v}" for v in items]
    if extra:
        lines.append(f"{indent}... (+{extra} more)")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Tool vs MDM QC Comparison
# ═══════════════════════════════════════════════════════════════════════════

def generate_qc_check(
    attribute_values_df: pd.DataFrame,
    attributes_df: pd.DataFrame,
    final_df: pd.DataFrame,
    debug: bool = False,
    valid_model_suffixes: Optional[Set[str]] = None,
) -> pd.DataFrame:
    """
    Compare attribute values between tool inputs and MDM output.

    TOOL side: Attributes.txt + AttributeValues.txt
    MDM side:  Values used in the FINAL output, filtered to columns whose
               name appears in Attributes.txt (the tool's source of truth).
               For multi-model projects, Attributes.txt files from all
               subdirectories are already appended in attributes_df before
               this function is called.

    When *valid_model_suffixes* is provided, suffixed columns whose suffix
    does not match the active models are excluded from the MDM comparison.

    Returns a DataFrame of mismatches with columns:
    Attribute_Name, NOT PRESENT IN TOOL, NOT PRESENT IN MDM.
    """
    # --- Tool-side: join attribute values to attribute names ----------------
    tool_attributes = attribute_values_df.merge(
        attributes_df, how="left", on="Attribute_Id"
    )[["Attribute_Name", "Attribute_Value"]]

    tool_values_by_attribute = (
        tool_attributes.groupby("Attribute_Name")["Attribute_Value"]
        .apply(list)
        .reset_index()
    )

    # --- MDM-side: extract values from FINAL using Attributes.txt to filter --
    # Only compare columns whose base name appears in the tool's Attributes.txt.
    # This naturally excludes META-only fields (DEMAND_GROUP, etc.) that the
    # tool does not own.  For multi-model projects, attributes_df is already
    # the concatenation of all subdirectory Attributes.txt files.
    valid_attribute_groups = set(attributes_df["Attribute_Name"].dropna().unique())

    # Build a suffix whitelist for model-suffixed column filtering
    suffix_whitelist = (
        {s.upper() for s in valid_model_suffixes}
        if valid_model_suffixes else None
    )

    # Build a lookup of upper-cased attribute group names for suffix matching
    ag_upper_map = {str(ag).upper(): ag for ag in valid_attribute_groups}

    # Map each qualifying column to its base attribute name.
    # Handles both exact matches (e.g. "FLAVOR") and suffixed variants
    # (e.g. "FLAVOR_MULO" → base "FLAVOR") so multi-model projects work.
    mdm_column_to_base: dict = {}  # actual_col → base_attribute_name

    for col_name in final_df.columns:
        col_upper = str(col_name).upper()

        # Skip BRAND/TOOL_BRAND/RPTG columns
        if col_upper.startswith("BRAND") or "RPTG" in col_upper:
            continue

        # Exact match against attribute groups
        if col_name in valid_attribute_groups:
            mdm_column_to_base[col_name] = col_name
            continue

        # Suffixed variant: check if col = base_SUFFIX where base is an
        # attribute group and SUFFIX is in the whitelist.
        # Only include suffixed columns when an explicit whitelist exists —
        # i.e. when multi-model subdirectories with their own Attributes /
        # AttributeValues files have been detected.  When no whitelist is
        # present (single-model run) we only compare exact-match columns;
        # _GROUP / _DETAIL columns are excluded because their valid value
        # sets will be defined by extended attribute files in future
        # multi-model runs.
        if suffix_whitelist is not None:
            for ag_upper, ag_original in ag_upper_map.items():
                prefix = ag_upper + "_"
                if col_upper.startswith(prefix):
                    suffix = col_upper[len(prefix):]
                    if suffix and suffix in suffix_whitelist:
                        mdm_column_to_base[col_name] = ag_original
                    break

    # Merge values from all model variants of the same base attribute
    mdm_merged: dict = {}  # base_attr → combined values list
    for actual_col, base_name in mdm_column_to_base.items():
        values = final_df[actual_col].tolist()
        if base_name in mdm_merged:
            mdm_merged[base_name].extend(values)
        else:
            mdm_merged[base_name] = values

    mdm_values_by_attribute = pd.DataFrame({
        "Attribute_Name": list(mdm_merged.keys()),
        "Attribute_Value": list(mdm_merged.values()),
    })

    # --- Combine tool vs MDM into a single comparison frame ----------------
    combined = tool_values_by_attribute.merge(
        mdm_values_by_attribute,
        how="outer",
        on="Attribute_Name",
        suffixes=("_TOOL", "_MDM"),
    )

    # --- Normalization helpers ---------------------------------------------
    def _normalize_list(raw_list):
        """Clean a list: remove None/NaN/empty/''."""
        if not isinstance(raw_list, list):
            return []
        return [
            str(v).strip() for v in raw_list
            if v is not None and str(v).strip() and str(v).strip().upper() != "NAN"
        ]

    def _to_upper_set(raw_list):
        """Convert a list to an uppercase set after normalization."""
        return set(s.upper() for s in _normalize_list(raw_list))

    # --- Compute differences per attribute ---------------------------------
    not_in_tool_list = []
    not_in_mdm_list = []

    if debug:
        print(f"\n{MAJOR_SEP}")
        print("QC DEBUG: RAW TOOL vs MDM VALUES")
        print(MAJOR_SEP)
        print(f"{INDENT}Total attributes to compare: {len(combined)}")

    for _, row in combined.iterrows():
        attribute_name = row["Attribute_Name"]
        tool_values_set = _to_upper_set(row["Attribute_Value_TOOL"])
        mdm_values_set = _to_upper_set(row["Attribute_Value_MDM"])

        not_in_tool = sorted(mdm_values_set - tool_values_set)
        not_in_mdm = sorted(tool_values_set - mdm_values_set)

        not_in_tool_list.append(not_in_tool)
        not_in_mdm_list.append(not_in_mdm)

        if debug:
            print(f"{INDENT}Attribute: {attribute_name}")
            print(f"{INDENT}  TOOL values ({len(tool_values_set)}): {sorted(tool_values_set)}")
            print(f"{INDENT}  MDM  values ({len(mdm_values_set)}): {sorted(mdm_values_set)}")
            print(f"{INDENT}  → NOT PRESENT IN TOOL: {not_in_tool}")
            print(f"{INDENT}  → NOT PRESENT IN MDM : {not_in_mdm}")

    combined["NOT PRESENT IN TOOL"] = not_in_tool_list
    combined["NOT PRESENT IN MDM"] = not_in_mdm_list

    # Only return attributes that have at least one mismatch
    result = combined[["Attribute_Name", "NOT PRESENT IN TOOL", "NOT PRESENT IN MDM"]]
    result = result[
        result["NOT PRESENT IN TOOL"].map(bool)
        | result["NOT PRESENT IN MDM"].map(bool)
    ].reset_index(drop=True)

    if debug:
        print(f"\n{MINOR_SEP}")
        print("QC DEBUG SUMMARY")
        print(MINOR_SEP)
        print(f"{INDENT}Attributes with mismatches: {len(result)}")
        if not result.empty:
            print(_indent_block(result.to_string(index=False), indent=INDENT))

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 Main Pipeline
# ═══════════════════════════════════════════════════════════════════════════

def aic_code(
    directory_path: str,
    df_override: pd.DataFrame = None,
    skip_qc: bool = False,
    file_manifest: Dict[str, Any] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, Optional[str]]:
    """
    Run the AIC Phase 2 pipeline on a directory of inputs.

    Handles both single-model (root-level tool files) and multi-model
    (subdirectory tool files, e.g. CONV/, MULO/) layouts.

    Parameters
    ----------
    directory_path : str
        Path to directory containing input files.
    df_override : DataFrame, optional
        If provided, use this for QC checks instead of building FINAL.
    skip_qc : bool, optional
        If True, skip the Tool vs MDM QC check (runs separately later).
    file_manifest : dict, optional
        Pre-scanned directory manifest from ``pipeline._scan_directory()``.
        When provided, skips directory scanning and file reads for items
        already in the manifest (workbook, tool files, JSON config).

    Returns
    -------
    (final_df, qc_results_df, meta_df, combined_attributes_df, combined_attr_values_df, demand_group_fallback)
    """

    # ==================================================================
    # Step 1: Scan Directory for Input Files
    # ==================================================================
    print(f"\n\n1) Scanning directory")

    if file_manifest:
        # Use pre-scanned manifest — skip redundant os.listdir calls
        parsed_files: Dict[str, Any] = {}
        json_config = file_manifest.get("json_config")
        skipped_files: List[str] = file_manifest.get("skipped_files", [])

        # Re-use the already-opened ExcelFile object
        if file_manifest.get("workbook_path"):
            wb_path = file_manifest["workbook_path"]
            wb_name = os.path.basename(str(wb_path))
            parsed_files[wb_name] = pd.ExcelFile(str(wb_path), engine="openpyxl")

        if skipped_files:
            print(f"{INDENT}Skipped {len(skipped_files)} non-essential file(s): {', '.join(skipped_files)}")

        # Use pre-loaded tool DataFrames
        combined_attributes_df = file_manifest.get("combined_attributes_df")
        combined_attr_values_df = file_manifest.get("combined_attr_values_df")
        tool_sources: List[str] = file_manifest.get("tool_sources", [])
    else:
        root_files = {}
        for file_name in os.listdir(directory_path):
            full_path = os.path.join(directory_path, file_name)
            if os.path.isfile(full_path):
                root_files[file_name] = full_path

        # Expected file patterns
        expected_txt_patterns = [
            r"^Attributes\.txt$",
            r"^AttributeValues\.txt$",
            r"(?i)^ModelInfo.*\.txt$",
        ]
        expected_xlsx_pattern = r"(?i).*file_for_mapping_qc.*\.xlsx$"

        def _is_expected_txt(name: str) -> bool:
            return any(re.match(p, name) for p in expected_txt_patterns)

        def _is_expected_xlsx(name: str) -> bool:
            return bool(re.match(expected_xlsx_pattern, name))

        # Read each file into an appropriate in-memory object
        parsed_files: Dict[str, Any] = {}
        json_config = None
        skipped_files: List[str] = []

        for file_name, file_path in root_files.items():
            try:
                if file_name.endswith(".csv"):
                    parsed_files[file_name] = pd.read_csv(file_path)

                elif file_name.endswith(".xlsx"):
                    if _is_expected_xlsx(file_name):
                        parsed_files[file_name] = pd.ExcelFile(file_path, engine="openpyxl")
                    else:
                        skipped_files.append(file_name)

                elif file_name.endswith(".txt"):
                    if _is_expected_txt(file_name):
                        parsed_files[file_name] = pd.read_csv(file_path, delimiter="|")
                    else:
                        skipped_files.append(file_name)

                elif file_name.endswith(".json"):
                    with open(file_path, "r") as f:
                        try:
                            json_config = json.load(f)
                            parsed_files[file_name] = json_config
                        except json.JSONDecodeError:
                            print(f"{INDENT}  ⚠ Skipped {file_name}: invalid JSON format")
                            skipped_files.append(file_name)
                        except Exception as exc:
                            print(f"{INDENT}  ⚠ Skipped {file_name}: {exc}")
                            skipped_files.append(file_name)
                else:
                    skipped_files.append(file_name)

            except PermissionError:
                raise PermissionError(
                    f"\n{'=' * 60}\n"
                    f"FILE LOCKED: Cannot access '{file_name}'\n"
                    f"{'=' * 60}\n"
                    f"The file appears to be open in another application (e.g., Excel).\n\n"
                    f"Please close the file and try again.\n"
                    f"{'=' * 60}"
                )
            except Exception as exc:
                print(f"{INDENT}  ⚠ Skipped {file_name}: could not parse ({type(exc).__name__})")
                skipped_files.append(file_name)

        if skipped_files:
            print(f"{INDENT}  Skipped {len(skipped_files)} non-essential file(s): {', '.join(skipped_files)}")

        # Collect tool files from root + subdirectories
        all_attributes_dfs: List[pd.DataFrame] = []
        all_attr_values_dfs: List[pd.DataFrame] = []
        tool_sources: List[str] = []

        attributes_keys = [k for k in parsed_files if re.match(r"^Attributes\.txt$", k)]
        attr_values_keys = [k for k in parsed_files if re.match(r"^AttributeValues\.txt$", k)]

        if attributes_keys and attr_values_keys:
            root_attrs_df = parsed_files[attributes_keys[0]]
            _log_brand_attribute_row(root_attrs_df, source="root/Attributes.txt")
            all_attributes_dfs.append(root_attrs_df)
            all_attr_values_dfs.append(parsed_files[attr_values_keys[0]])
            tool_sources.append("root")

        for entry_name in os.listdir(directory_path):
            subdir_path = os.path.join(directory_path, entry_name)
            if not os.path.isdir(subdir_path):
                continue

            sub_files = {name: os.path.join(subdir_path, name) for name in os.listdir(subdir_path)}
            attr_path = sub_files.get("Attributes.txt")
            attr_val_path = sub_files.get("AttributeValues.txt")

            if attr_path and attr_val_path:
                try:
                    sub_attrs_df = pd.read_csv(attr_path, delimiter="|")
                    _log_brand_attribute_row(sub_attrs_df, source=f"{entry_name}/Attributes.txt")
                    all_attributes_dfs.append(sub_attrs_df)
                    all_attr_values_dfs.append(pd.read_csv(attr_val_path, delimiter="|"))
                    tool_sources.append(f"subdir:{entry_name}")
                except Exception as exc:
                    print(f"{INDENT}Error reading tool files in subdirectory '{entry_name}': {exc}")

        combined_attributes_df = None
        combined_attr_values_df = None
        if all_attributes_dfs and all_attr_values_dfs:
            combined_attributes_df = pd.concat(all_attributes_dfs, ignore_index=True).drop_duplicates()
            combined_attr_values_df = pd.concat(all_attr_values_dfs, ignore_index=True).drop_duplicates()

    # ==================================================================
    # Step 2: Parse Excel Workbook (FLAT_FILE, META, FINAL)
    # ==================================================================
    print(f"\n\n2) Parsing workbook (FLAT_FILE, META, FINAL)")

    # Resolve config values from JSON
    brand_column_name = json_config.get("brandCol", "") if json_config else ""
    pl_name = json_config.get("PLName", "") if json_config else ""

    # Locate the mapping workbook
    expected_xlsx_pattern = r"(?i).*file_for_mapping_qc.*\.xlsx$"
    workbook_keys = [k for k in parsed_files if re.match(expected_xlsx_pattern, k)]

    workbook = parsed_files[workbook_keys[0]] if workbook_keys else None

    if not workbook:
        raise FileNotFoundError(
            "No File_For_Mapping_QC*.xlsx found in directory. "
            "Expected a file containing 'file_for_mapping_qc' in its name."
        )

    # --- Flexible sheet parsing helper -------------------------------------
    def _parse_sheet(excel_file: pd.ExcelFile, target_name: str, **kwargs) -> pd.DataFrame:
        """Parse a sheet by name using case-insensitive fuzzy matching."""
        def _normalize_name(name: str) -> str:
            return name.strip().lower().replace("_", " ").replace("-", " ")

        def _drop_index_columns(df: pd.DataFrame) -> pd.DataFrame:
            """Remove columns with numeric or 'Unnamed: N' headers."""
            if df.empty:
                return df
            columns_to_drop = []
            for col in df.columns:
                col_str = str(col).strip()
                if col_str.isdigit() or re.match(r"^Unnamed:\s*\d+$", col_str, re.IGNORECASE):
                    columns_to_drop.append(col)
            if columns_to_drop:
                print(f"{INDENT}  Dropped index-like columns from {target_name}: {columns_to_drop}")
                df = df.drop(columns=columns_to_drop)
            return df

        sheet_lookup = {_normalize_name(name): name for name in excel_file.sheet_names}
        actual_name = sheet_lookup.get(_normalize_name(target_name))
        if not actual_name:
            return pd.DataFrame()
        df = excel_file.parse(actual_name, **kwargs)
        return _drop_index_columns(df)

    # Parse the three required sheets
    flat_file_df = _parse_sheet(workbook, "FLAT_FILE", index_col=None)
    meta_df = _parse_sheet(workbook, "META", index_col=None)
    template_df = _parse_sheet(workbook, "FINAL", index_col=None)
    template_columns = template_df.columns

    print(
        f"{INDENT}{workbook_keys[0]} → FLAT_FILE {len(flat_file_df)}×{len(flat_file_df.columns)}, "
        f"META {len(meta_df)} attrs, FINAL {len(template_columns)} cols"
    )

    # ==================================================================
    # Step 3: Early Duplicate ITEM_DIM_KEY Check
    # Catches bad data (appended categories, manual errors) before
    # attribute processing. Only deduplicates on ITEM_DIM_KEY —
    # rows with same SKU but different dim keys are preserved.
    # ==================================================================
    print(f"\n\n3) Duplicate ITEM_DIM_KEY check")

    col_upper_map = {str(c).upper(): c for c in flat_file_df.columns}
    dimkey_col = col_upper_map.get("ITEM_DIM_KEY")
    dollars_col = col_upper_map.get("RAW_TOTAL_DOLLARS")
    pre_dedup_row_count = len(flat_file_df)

    if dimkey_col:
        dimkey_strings = flat_file_df[dimkey_col].astype(str).str.strip()
        valid_dimkey_mask = flat_file_df[dimkey_col].notna() & dimkey_strings.ne("")
        dimkey_value_counts = dimkey_strings[valid_dimkey_mask].value_counts()
        duplicate_dimkeys = dimkey_value_counts[dimkey_value_counts > 1]

        if not duplicate_dimkeys.empty:
            num_duplicate_keys = len(duplicate_dimkeys)
            pre_unique_count = dimkey_strings[valid_dimkey_mask].nunique()

            if dollars_col:
                # Keep the row with highest RAW_TOTAL_DOLLARS for each duplicate key
                dollar_values = pd.to_numeric(flat_file_df[dollars_col], errors="coerce").fillna(float("-inf"))
                is_duplicate = valid_dimkey_mask & dimkey_strings.isin(set(duplicate_dimkeys.index))
                rows_to_keep = dollar_values[is_duplicate].groupby(dimkey_strings[is_duplicate], sort=False).idxmax()
                rows_to_drop = is_duplicate & ~flat_file_df.index.isin(rows_to_keep.values)
                dropped_count = int(rows_to_drop.sum())
                flat_file_df = flat_file_df.loc[~rows_to_drop].copy()

                # Validate no unique dim keys were lost
                post_unique_count = (
                    flat_file_df[dimkey_col].astype(str).str.strip()[flat_file_df[dimkey_col].notna()].nunique()
                )

                print(f"{INDENT}Found {num_duplicate_keys} duplicate ITEM_DIM_KEYs — dropped {dropped_count} rows ({pre_dedup_row_count} → {len(flat_file_df)})")
                if post_unique_count < pre_unique_count:
                    lost_count = pre_unique_count - post_unique_count
                    print(f"{INDENT}⚠ {lost_count} unique ITEM_DIM_KEYs lost during dedup — investigate input data")
                else:
                    print(f"{INDENT}All {pre_unique_count} unique ITEM_DIM_KEYs preserved (kept highest RAW_TOTAL_DOLLARS)")
                print(f"{INDENT}Please cross-check input files against output to verify data integrity.")
                print(f"{INDENT}Duplicates may indicate manual copy errors when combining multiple categories.")
            else:
                print(f"{INDENT}⚠ {num_duplicate_keys} duplicate ITEM_DIM_KEYs found but no RAW_TOTAL_DOLLARS to resolve")
                print(f"{INDENT}  Deferred to Phase 3 Step 10.")
        else:
            print(f"{INDENT}✓ No duplicate ITEM_DIM_KEYs found")
    else:
        print(f"{INDENT}⚠ ITEM_DIM_KEY column not found — check data inputs")

    # Track original dimensions for integrity checks later
    original_row_count = len(flat_file_df)
    original_col_count = len(flat_file_df.columns)

    # ==================================================================
    # Step 4: Attribute Assembly (MODELING / REPORTING)
    # ==================================================================
    print(f"\n\n4) Attribute assembly (MODELING / REPORTING)")

    # Classify each attribute group as MODELING or REPORTING based on META
    attribute_type_map: Dict[str, str] = {}
    attribute_key_cols_map: Dict[str, List[str]] = {}

    for attribute_group in meta_df["Attribute Group name"].unique():
        meta_subset = meta_df[meta_df["Attribute Group name"] == attribute_group]
        attribute_types = meta_subset["Attribute_Type"].unique()

        if len(attribute_types) == 1 and attribute_types[0] == "MODELING":
            attribute_type_map[attribute_group] = "MODELING"
            attribute_key_cols_map[attribute_group] = meta_subset["Attribute Name in MDM"].to_list()
        elif "MODELING" not in attribute_types:
            attribute_type_map[attribute_group] = "REPORTING"
            attribute_key_cols_map[attribute_group] = meta_subset["Attribute Name in MDM"].to_list()

    modeling_count = sum(1 for t in attribute_type_map.values() if t == "MODELING")
    reporting_count = sum(1 for t in attribute_type_map.values() if t == "REPORTING")

    # Track columns already converted to string (avoid redundant conversions)
    string_converted_cols: set = set()

    # New values discovered during attribute assembly (for QC logging)
    new_attribute_values: Dict[str, List[Any]] = {}

    for attribute_group, attribute_type in attribute_type_map.items():
        if attribute_type == "MODELING":
            # --- MODELING: left-join FLAT_FILE with lookup sheet on key columns ---
            key_columns = attribute_key_cols_map[attribute_group]

            try:
                _lkp_raw = workbook.parse(
                    f"Final_{attribute_group}_lkp"[:31],
                    index=False,
                )
            except Exception:
                # Try with underscored variant of sheet name
                attribute_group_underscore = attribute_group.replace(" ", "_")
                _lkp_raw = workbook.parse(
                    f"Final_{attribute_group_underscore}_lkp"[:31],
                    index=False,
                )

            _want_cols = key_columns + [attribute_group, "Rank"]
            _has_ml_suggestion = "ML Suggestion" in _lkp_raw.columns
            if _has_ml_suggestion:
                _want_cols = _want_cols + ["ML Suggestion"]
            lookup_df = _lkp_raw[_want_cols].drop_duplicates()

            lookup_df.rename(columns={attribute_group: f"{attribute_group}_Match"}, inplace=True)
            if _has_ml_suggestion:
                lookup_df.rename(columns={"ML Suggestion": f"{attribute_group}_ML"}, inplace=True)

            # Ensure key columns are string type for reliable merging.
            # Both sides must be normalised to the same case: Ensemble.py
            # uppercases lkp key columns (.str.upper()), but FLAT_FILE key
            # columns come from attrGridPDF.fillna('nan') which is lowercase.
            # Without upper() here, 'nan' (FLAT_FILE) != 'NAN' (lkp) and
            # left-join silently returns NaN for every null-keyed row.
            for key_col in key_columns:
                lookup_df[key_col] = lookup_df[key_col].astype("str").str.strip().str.upper()
                if key_col not in string_converted_cols:
                    flat_file_df[key_col] = flat_file_df[key_col].astype("str").str.strip().str.upper()
                    string_converted_cols.add(key_col)

            # Filter to Rank=1 before merging (more efficient)
            rank1_lookup = lookup_df[lookup_df["Rank"] == 1].drop(columns=["Rank"])

            # Validate: no duplicate key combinations that would cause cross-join
            _check_for_duplicate_keys(
                lookup_df=rank1_lookup,
                key_cols=key_columns,
                value_col=f"{attribute_group}_Match",
                attribute_name=attribute_group,
                flat_file_df=flat_file_df,
                output_dir=directory_path,
            )

            # Left-join: bring lookup values into FLAT_FILE
            flat_file_df = pd.merge(
                flat_file_df,
                rank1_lookup,
                on=key_columns,
                how="left",
            )

            # Fill empty/NaN values with the lookup match
            try:
                temp_set = set(
                    np.where(
                        ((flat_file_df[attribute_group]) | (flat_file_df[attribute_group].isna()))
                        & (~flat_file_df[f"{attribute_group}_Match"].isin(flat_file_df[attribute_group])),
                        flat_file_df[f"{attribute_group}_Match"],
                        "no new value",
                    )
                )
                if len(temp_set) > 1:
                    temp_set.discard("no new value")
                new_attribute_values[attribute_group] = list(temp_set)

                # Treat "", NaN, and the literal string "nan" (written by
                # Phase 1's attrGridPDF.fillna('nan')) all as unfilled.
                _is_empty = (
                    (flat_file_df[attribute_group] == "")
                    | flat_file_df[attribute_group].isna()
                    | (flat_file_df[attribute_group].astype(str).str.lower() == "nan")
                )
                flat_file_df[attribute_group] = np.where(
                    _is_empty,
                    flat_file_df[f"{attribute_group}_Match"],
                    flat_file_df[attribute_group],
                )

                # ML Suggestion fallback: if lookup returned nothing and an ML
                # suggestion exists, use it to avoid leaving the cell blank.
                if _has_ml_suggestion and f"{attribute_group}_ML" in flat_file_df.columns:
                    _ml_col = f"{attribute_group}_ML"
                    _still_empty = (
                        (flat_file_df[attribute_group] == "")
                        | flat_file_df[attribute_group].isna()
                        | (flat_file_df[attribute_group].astype(str).str.lower() == "nan")
                    )
                    _ml_valid = (
                        flat_file_df[_ml_col].notna()
                        & (flat_file_df[_ml_col] != "")
                        & (flat_file_df[_ml_col].astype(str).str.lower() != "nan")
                    )
                    flat_file_df[attribute_group] = np.where(
                        _still_empty & _ml_valid,
                        flat_file_df[_ml_col],
                        flat_file_df[attribute_group],
                    )
            except Exception:
                flat_file_df[attribute_group] = flat_file_df[f"{attribute_group}_Match"]

            _drop_cols = [f"{attribute_group}_Match"]
            if _has_ml_suggestion and f"{attribute_group}_ML" in flat_file_df.columns:
                _drop_cols.append(f"{attribute_group}_ML")
            flat_file_df.drop(columns=_drop_cols, inplace=True)

        elif attribute_type == "REPORTING":
            # --- REPORTING: direct copy from MDM key columns in FLAT_FILE ---
            new_attribute_values[attribute_group] = ["no new value"]
            source_columns = attribute_key_cols_map[attribute_group]
            flat_file_df[attribute_group] = flat_file_df[source_columns]

    print(
        f"{INDENT}✓ Processed {modeling_count} MODELING + {reporting_count} REPORTING attributes "
        f"({modeling_count} lookup sheets validated)"
    )

    # Verify row count was not altered by MODELING attribute merges
    if len(flat_file_df) != original_row_count:
        explosion_factor = len(flat_file_df) / original_row_count
        print(f"{INDENT}⚠ Row count changed during attribute merges: {original_row_count} → {len(flat_file_df)} ({explosion_factor:.2f}x)")
        print(f"{INDENT}  Check lookup sheets for duplicate key combinations")

    # ==================================================================
    # Step 5: Build Output DataFrame
    # Maps FLAT_FILE columns to the FINAL template layout using fuzzy
    # matching. Special character replacement and SKU collapsing are
    # handled downstream in Phase 3 (quality.py, sku_collapse.py).
    # ==================================================================
    print(f"\n\n5) Building output dataframe")

    # Pre-compute column mappings (avoid repeated fuzzy matching)
    flat_file_col_names = list(flat_file_df.columns)
    column_mapping: Dict[str, str] = {}
    for col_name in template_columns[1:]:
        matches = difflib.get_close_matches(col_name, flat_file_col_names, n=1, cutoff=0.6)
        column_mapping[col_name] = matches[0] if matches else None

    mapped_count = sum(1 for v in column_mapping.values() if v is not None)
    unmapped_columns = [k for k, v in column_mapping.items() if v is None]
    if unmapped_columns:
        print(f"{INDENT}⚠ {len(unmapped_columns)} unmapped columns: {unmapped_columns[:10]}")

    # Build the output DataFrame column by column
    output_df = pd.DataFrame()
    output_df["UPDATE_REQUIRED"] = 1
    columns_failed = 0

    for col_name in template_columns[1:]:
        source_col = column_mapping.get(col_name)
        if not source_col:
            output_df[col_name] = "Not in Old PassThrough or AttrGrid"
            columns_failed += 1
            continue
        try:
            output_df[col_name] = (
                flat_file_df[source_col]
                .replace(np.nan, "")
                .replace("nan", "")
            )
        except Exception as col_error:
            output_df[col_name] = "Not in Old PassThrough or AttrGrid"
            columns_failed += 1
            print(f"{INDENT}⚠ Failed to copy '{col_name}' from '{source_col}': {col_error}")

    built_summary = (
        f"{output_df.shape[0]}×{output_df.shape[1]} "
        f"({mapped_count} mapped, {columns_failed} unmapped)"
    )

    if len(output_df) != len(flat_file_df):
        print(f"{INDENT}⚠ Row count mismatch: output ({len(output_df)}) != FLAT_FILE ({len(flat_file_df)})")

    final_df = output_df.copy()

    # Copy RAW_DESCRIPTION to DESCRIPTION if present
    try:
        final_df["DESCRIPTION"] = final_df["RAW_DESCRIPTION"]
    except KeyError:
        pass

    final_df["UPDATE_REQUIRED"] = 1

    # --- Validate output integrity against raw input -----------------------
    integrity_issues: List[str] = []

    if len(final_df) != len(flat_file_df):
        integrity_issues.append(f"Row count: FINAL ({len(final_df)}) != FLAT_FILE ({len(flat_file_df)})")

    if len(final_df.columns) != len(template_columns):
        integrity_issues.append(f"Column count: FINAL ({len(final_df.columns)}) != template ({len(template_columns)})")

    # Check no ITEM_DIM_KEYs were lost during mapping
    if dimkey_col and dimkey_col in flat_file_df.columns:
        input_dimkeys = set(flat_file_df[dimkey_col].dropna().astype(str).str.strip().unique())
        final_dimkey_col = next(
            (c for c in final_df.columns if str(c).upper() == "ITEM_DIM_KEY"), None
        )
        if final_dimkey_col:
            output_dimkeys = set(final_df[final_dimkey_col].dropna().astype(str).str.strip().unique())
            lost_dimkeys = input_dimkeys - output_dimkeys
            if lost_dimkeys:
                integrity_issues.append(f"Lost {len(lost_dimkeys)} ITEM_DIM_KEYs not in output")

    if integrity_issues:
        print(f"{INDENT}✓ Built output: {built_summary}")
        print(f"{INDENT}⚠ Integrity warnings:")
        for issue in integrity_issues:
            print(f"{INDENT}  - {issue}")
    else:
        print(f"{INDENT}✓ Built output: {built_summary} — DimKeys preserved")

    # Use df_override for QC if provided (for post-processing QC)
    qc_target_df = df_override if df_override is not None else final_df

    # ==================================================================
    # Step 6: Finalize Phase 2 Output -> tool v mdm qc's now moved to bottom of pipeline post-phase 3
    # Keeping here for backwards compatibility or if someone wants to include these qc's at beginning also (change argument in pipeline)
    # ==================================================================

    if skip_qc:
        qc_results_df = pd.DataFrame(
            columns=["Attribute_Name", "NOT PRESENT IN TOOL", "NOT PRESENT IN MDM"]
        )
    elif combined_attributes_df is None or combined_attr_values_df is None:
        print(f"{INDENT}No Attributes.txt / AttributeValues.txt files found (root or subdirs). Skipping QC.")
        qc_results_df = pd.DataFrame(
            columns=["Attribute_Name", "NOT PRESENT IN TOOL", "NOT PRESENT IN MDM"]
        )
    else:
        # Run unified QC (works for both single-model and multi-model)
        print(f"\n{INDENT}Tool vs MDM Attribute QC")
        print(f"{INDENT}{MINOR_SEP}")
        print(f"{INDENT}Sources loaded: {', '.join(tool_sources)}")
        print(f"{INDENT}Total attributes: {combined_attributes_df['Attribute_Name'].nunique()}")
        print(f"{INDENT}Total attribute values: {len(combined_attr_values_df)}")

        tool_attribute_names = set(combined_attributes_df["Attribute_Name"].dropna().unique())
        print(f"{INDENT}Attributes in tool files:")
        for attr_name in sorted(tool_attribute_names):
            print(f"{INDENT}  • {attr_name}")

        qc_results_df = generate_qc_check(
            combined_attr_values_df,
            combined_attributes_df,
            qc_target_df,
        )

        if qc_results_df.empty:
            print(f"\n{INDENT}✓ No mismatches found between Tool and MDM values.")
        else:
            print(f"\n{INDENT}⚠ Mismatches found ({len(qc_results_df)} attribute(s)):")
            for _, row in qc_results_df.iterrows():
                attr_name = row["Attribute_Name"]
                not_in_tool = row["NOT PRESENT IN TOOL"]
                not_in_mdm = row["NOT PRESENT IN MDM"]

                print(f"\n{INDENT}  {attr_name}")
                if _is_nonempty(not_in_tool):
                    print(f"{INDENT}    NOT IN TOOL: {_fmt_list_cell(not_in_tool)}")
                if _is_nonempty(not_in_mdm):
                    print(f"{INDENT}    NOT IN MDM:  {_fmt_list_cell(not_in_mdm)}")

    # --- Extract DEMAND_GROUP fallback from FINAL template -----------------
    # If FLAT_FILE had all-blank DEMAND_GROUP, the FINAL tab may still have
    # the correct value from a prior run or analyst input.
    demand_group_fallback = None
    dg_template_col = next(
        (c for c in template_df.columns if str(c).upper() == "DEMAND_GROUP"), None
    )
    if dg_template_col and not template_df.empty:
        dg_values = (
            template_df[dg_template_col]
            .dropna()
            .astype(str)
            .str.strip()
            .loc[lambda s: s.ne("")]
            .unique()
        )
        if len(dg_values) == 1:
            demand_group_fallback = dg_values[0]

    return final_df, qc_results_df, meta_df, combined_attributes_df, combined_attr_values_df, demand_group_fallback


# ═══════════════════════════════════════════════════════════════════════════
# Post-Transformation QC (called from pipeline.py step 14)
# ═══════════════════════════════════════════════════════════════════════════

def run_tool_vs_mdm_qc(
    directory_path: str,
    df_to_check: pd.DataFrame,
    combined_attributes_df: pd.DataFrame = None,
    combined_attr_values_df: pd.DataFrame = None,
    valid_model_suffixes: Optional[Set[str]] = None,
) -> pd.DataFrame:
    """
    Run Tool vs MDM attribute QC on a post-transformation DataFrame.

    Reads Attributes.txt / AttributeValues.txt from the directory
    (root + subdirectories) and compares attribute values against the
    provided DataFrame. Use this AFTER all pipeline transformations.

    The set of attributes to compare is driven entirely by Attributes.txt —
    META is not consulted, so dimension/reporting fields such as DEMAND_GROUP
    are naturally excluded.  For multi-model projects the Attributes.txt
    files from each subdirectory are appended before comparison.

    Parameters
    ----------
    directory_path : str
        Path to directory containing input files.
    df_to_check : DataFrame
        Post-transformation DataFrame to QC.
    combined_attributes_df : DataFrame, optional
        Pre-loaded combined Attributes.txt data. If provided along with
        combined_attr_values_df, skips directory scanning.
    combined_attr_values_df : DataFrame, optional
        Pre-loaded combined AttributeValues.txt data.

    Returns a QC results DataFrame with columns:
    Attribute_Name, NOT PRESENT IN TOOL, NOT PRESENT IN MDM.
    """
    print(f"\n{INDENT}Tool vs MDM Attribute QC (Post-Transformation)")
    print(f"{INDENT}{MINOR_SEP}")

    # Use pre-loaded tool DataFrames if available, otherwise scan directory
    if combined_attributes_df is not None and combined_attr_values_df is not None:
        tool_sources = ["pre-loaded"]
    else:
        # --- Collect tool files from root + subdirectories ---------------------
        all_attributes_dfs: List[pd.DataFrame] = []
        all_attr_values_dfs: List[pd.DataFrame] = []
        tool_sources: List[str] = []

        # Root-level files
        root_attr_path = os.path.join(directory_path, "Attributes.txt")
        root_attr_val_path = os.path.join(directory_path, "AttributeValues.txt")

        if os.path.isfile(root_attr_path) and os.path.isfile(root_attr_val_path):
            try:
                all_attributes_dfs.append(pd.read_csv(root_attr_path, delimiter="|"))
                all_attr_values_dfs.append(pd.read_csv(root_attr_val_path, delimiter="|"))
                tool_sources.append("root")
            except Exception as exc:
                print(f"{INDENT}  ⚠ Error reading root tool files: {exc}")

        # Subdirectory files
        for entry_name in os.listdir(directory_path):
            subdir_path = os.path.join(directory_path, entry_name)
            if not os.path.isdir(subdir_path):
                continue

            attr_path = os.path.join(subdir_path, "Attributes.txt")
            attr_val_path = os.path.join(subdir_path, "AttributeValues.txt")

            if os.path.isfile(attr_path) and os.path.isfile(attr_val_path):
                try:
                    all_attributes_dfs.append(pd.read_csv(attr_path, delimiter="|"))
                    all_attr_values_dfs.append(pd.read_csv(attr_val_path, delimiter="|"))
                    tool_sources.append(f"subdir:{entry_name}")
                except Exception as exc:
                    print(f"{INDENT}  ⚠ Error reading tool files in '{entry_name}': {exc}")

        if not all_attributes_dfs or not all_attr_values_dfs:
            print(f"{INDENT}No Attributes.txt / AttributeValues.txt files found. Skipping QC.")
            return pd.DataFrame(columns=["Attribute_Name", "NOT PRESENT IN TOOL", "NOT PRESENT IN MDM"])

        combined_attributes_df = pd.concat(all_attributes_dfs, ignore_index=True).drop_duplicates()
        combined_attr_values_df = pd.concat(all_attr_values_dfs, ignore_index=True).drop_duplicates()

    print(f"{INDENT}Sources loaded: {', '.join(tool_sources)}")
    print(f"{INDENT}Total attributes: {combined_attributes_df['Attribute_Name'].nunique()}")
    print(f"{INDENT}Total attribute values: {len(combined_attr_values_df)}")

    tool_attribute_names = set(combined_attributes_df["Attribute_Name"].dropna().unique())
    print(f"{INDENT}Attributes in tool files:")
    for attr_name in sorted(tool_attribute_names):
        print(f"{INDENT}  • {attr_name}")

    # --- Run QC check ------------------------------------------------------
    qc_results = generate_qc_check(
        combined_attr_values_df,
        combined_attributes_df,
        df_to_check,
        valid_model_suffixes=valid_model_suffixes,
    )

    if qc_results.empty:
        print(f"\n{INDENT}✓ No mismatches found between Tool and MDM values.")
    else:
        print(f"\n{INDENT}⚠ Mismatches found ({len(qc_results)} attribute(s)):")
        for _, row in qc_results.iterrows():
            attr_name = row["Attribute_Name"]
            not_in_tool = row["NOT PRESENT IN TOOL"]
            not_in_mdm = row["NOT PRESENT IN MDM"]

            print(f"\n{INDENT}  {attr_name}")
            if _is_nonempty(not_in_tool):
                print(f"{INDENT}    NOT IN TOOL: {_fmt_list_cell(not_in_tool)}")
            if _is_nonempty(not_in_mdm):
                print(f"{INDENT}    NOT IN MDM:  {_fmt_list_cell(not_in_mdm)}")

        print(f"\n{INDENT}If values above are unexpected: double-check config dialogs and re-run, "
              f"or clean the values directly in the exported Excel file before pressing Finalize and Export.")

    return qc_results


# ═══════════════════════════════════════════════════════════════════════════
# QC Result Printing (used by external callers)
# ═══════════════════════════════════════════════════════════════════════════

def _print_phase2_qc(
    qc_df: pd.DataFrame,
    *,
    max_attrs: int = 50,
    max_items: int = 30,
) -> None:
    """
    Print actionable QC rows in a readable block-per-attribute format.

    Shows one section per mismatched attribute with counts and bulleted
    lists of differences, truncated for very long lists.
    """
    if qc_df is None or qc_df.empty:
        print(f"{INDENT}✓ No mismatches between tool and MDM for any model/suffix.")
        return

    required_columns = ["Attribute_Name", "NOT PRESENT IN TOOL", "NOT PRESENT IN MDM"]
    missing_columns = [c for c in required_columns if c not in qc_df.columns]
    if missing_columns:
        print(f"{INDENT}⚠ Phase 2 QC output missing expected columns: {missing_columns}")
        print(_indent_block(qc_df.head(max_attrs).to_string(index=False), indent=INDENT))
        if len(qc_df) > max_attrs:
            print(f"{INDENT}... and {len(qc_df) - max_attrs} more row(s).")
        return

    # Filter to rows that have at least one mismatch
    actionable = qc_df[
        qc_df["NOT PRESENT IN TOOL"].map(_is_nonempty)
        | qc_df["NOT PRESENT IN MDM"].map(_is_nonempty)
    ].copy()

    if actionable.empty:
        print(f"{INDENT}✓ No mismatches between tool and MDM for any model/suffix.")
        return

    actionable = actionable.sort_values("Attribute_Name", kind="stable").reset_index(drop=True)
    print(f"{INDENT}⚠ TOOL vs MDM Attribute QC: {len(actionable)} attribute(s) to review")

    shown = 0
    for _, row in actionable.iterrows():
        if shown >= max_attrs:
            remaining = len(actionable) - shown
            print(f"{INDENT}... {remaining} more attribute(s) not shown (increase max_attrs to view).")
            break

        attribute_name = str(row["Attribute_Name"])
        not_in_tool = _as_clean_list(row["NOT PRESENT IN TOOL"])
        not_in_mdm = _as_clean_list(row["NOT PRESENT IN MDM"])

        print(f"\n{INDENT}{MINOR_SEP}")
        print(f"{INDENT}Attribute: {attribute_name}")
        print(f"{INDENT}{MINOR_SEP}")

        print(f"{INDENT}Missing in TOOL: {len(not_in_tool)}")
        print(_fmt_bullets(not_in_tool, indent=INDENT * 2, max_items=max_items))

        print(f"\n{INDENT}Missing in MDM : {len(not_in_mdm)}")
        print(_fmt_bullets(not_in_mdm, indent=INDENT * 2, max_items=max_items))

        shown += 1
