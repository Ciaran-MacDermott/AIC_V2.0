"""
Synthetic fixture builder for Phase 1 + Phase 2 integration tests.

Phase 1
-------
Produces a (xlsx, csv) pair that mirrors the column layout the real
ml_package expects:

  META   sheet  — defines MODELING attributes and their RAW key columns
  FINAL  sheet  — historical labelled data (training reference)
  CSV    file   — new products to classify

The fixture follows the production META convention: the *key columns*
listed in ``Attribute Name in MDM`` are the raw input fields the lookup
keys off (e.g. ITEM_DESC, BRAND_RAW), and the *label column* is the
``Attribute Group name`` value (e.g. BRAND). Multiple META rows can
share the same group name to compose a multi-column key.

Phase 2
-------
Produces the four-file Phase 2 input set (File_For_Mapping_QC.xlsx +
ModelInfo.txt + Attributes.txt + AttributeValues.txt) plus a zip helper
that wraps them, so the test suite can exercise both the zip-upload
mode and the loose-files mode without touching the network.

Both fixtures are deliberately small — enough rows for every pipeline
stage to produce non-empty output without making the integration suite
take longer than ~60s on a quiet laptop.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd
import xlsxwriter  # noqa: F401  — required by pd.ExcelWriter(engine="xlsxwriter")


# Composite key per attribute: (raw description, raw value).
ATTRIBUTES = [
    {"label": "BRAND",     "key_cols": ["ITEM_DESC", "BRAND_RAW"]},
    {"label": "PACK_SIZE", "key_cols": ["ITEM_DESC", "PACK_RAW"]},
]

# Distinct (label, raw_brand, raw_pack) combos in the historical data.
HISTORY_COMBOS = [
    # (BRAND label, BRAND_RAW, PACK_SIZE label, PACK_RAW)
    ("ACME",  "acme co",   "12 OZ", "twelve ounce"),
    ("ACME",  "acme inc",  "24 OZ", "twenty four oz"),
    ("ZETA",  "zeta foods", "12 OZ", "twelve ounce"),
    ("ZETA",  "zeta",       "16 OZ", "sixteen oz"),
    ("OMEGA", "omega ltd",  "8 OZ",  "eight ounce"),
    ("OMEGA", "omega",      "32 OZ", "thirty two oz"),
]

# Repeats per combo — XGBoost needs at least 2 examples per class to train,
# BM25 IDF benefits from more.  10 keeps the test under ~30s on a quiet box.
HISTORY_REPEATS_PER_COMBO = 10

# New products to classify. Each row exercises a different match path.
NEW_PRODUCTS = [
    # Exact key match against history → lookup wins.
    {"ITEM_DESC": "acme co",     "BRAND_RAW": "acme co",     "PACK_RAW": "twelve ounce"},
    {"ITEM_DESC": "zeta foods",  "BRAND_RAW": "zeta foods",  "PACK_RAW": "twelve ounce"},
    {"ITEM_DESC": "omega ltd",   "BRAND_RAW": "omega ltd",   "PACK_RAW": "eight ounce"},

    # Same brand keyword but novel description text → BM25 should help.
    {"ITEM_DESC": "premium acme",   "BRAND_RAW": "acme co",   "PACK_RAW": "twelve ounce"},
    {"ITEM_DESC": "value zeta",     "BRAND_RAW": "zeta",      "PACK_RAW": "sixteen oz"},

    # Slight key drift — fuzzy + ensemble territory.
    {"ITEM_DESC": "acme",        "BRAND_RAW": "acme",        "PACK_RAW": "twenty four oz"},
    {"ITEM_DESC": "omega plus",  "BRAND_RAW": "omega",       "PACK_RAW": "thirty two oz"},
    {"ITEM_DESC": "zeta deluxe", "BRAND_RAW": "zeta foods",  "PACK_RAW": "twelve ounce"},
]


def build_meta_df() -> pd.DataFrame:
    """META sheet — one row per (key column, attribute group) pair."""
    rows: list[dict] = []
    for attr in ATTRIBUTES:
        for key_col in attr["key_cols"]:
            rows.append({
                "Attribute Name in MDM": key_col,
                "Attribute Group name":  attr["label"],
                "Attribute_Type":        "MODELING",
                "Type":                  "",
            })
    return pd.DataFrame(rows)


def build_history_df() -> pd.DataFrame:
    """
    FINAL sheet — historical products with known BRAND + PACK_SIZE labels.

    Each combo is repeated several times so:
      - Lookup has multiple sales rows per (key, label) to aggregate.
      - BM25 has a non-trivial corpus per label after stopword filtering.
      - XGBoost has at least 2 training rows per class.
    """
    rows: list[dict] = []
    item_id = 1
    for brand_label, brand_raw, pack_label, pack_raw in HISTORY_COMBOS:
        for n in range(HISTORY_REPEATS_PER_COMBO):
            rows.append({
                # NOTE: phase3_package skips the FIRST column of the FINAL
                # template (it's used implicitly for the UPDATE_REQUIRED
                # default), so the first column here intentionally is
                # UPDATE_REQUIRED so that ITEM_DIM_KEY survives the mapping.
                "UPDATE_REQUIRED":                  1,
                "ITEM_DIM_KEY":                     item_id,
                "ITEM_DESC":                        brand_raw,
                "BRAND_RAW":                        brand_raw,
                "PACK_RAW":                         pack_raw,
                "BRAND":                            brand_label,
                "PACK_SIZE":                        pack_label,
                "RAW_TOTAL_DOLLARS":                100.0 + n,
                # Phase 2 / phase3_package template columns ─ kept here so
                # the FINAL sheet (Phase 1's column template) carries them
                # through to Phase 2's column-mapping step.
                "ASSORTMENT_CATEGORY_DEFINITION":   "AMMO",
                "DEMAND_GROUP":                     "AMMO",
                "DESCRIPTION":                      brand_raw,
                "UPC10":                            f"{2000000000 + item_id:010d}",
                "SKU":                              f"SKU{item_id:05d}",
                "RAW_BRAND":                        brand_label,
                "RAW_MANUFACTURER":                 brand_label + " MFR",
                "RAW_ASSORTMENT_CATEGORY":          "AMMO",
                "RAW_US_MULTI_RETAILER_RESTRICTED": "",
                "TOOL_BRAND":                       brand_label,
            })
            item_id += 1
    return pd.DataFrame(rows)


def build_flat_file_df() -> pd.DataFrame:
    """CSV flat file — new products to classify.

    Includes the Phase 2 / phase3_package columns so that after Phase 1
    writes its FLAT_FILE_OUT sheet, the downstream pipeline finds the
    columns it expects (ASSORTMENT_CATEGORY_DEFINITION, DEMAND_GROUP,
    UPC10, RAW_*, etc.).
    """
    return pd.DataFrame([
        {
            "ITEM_DIM_KEY":                     1000 + idx,
            "ITEM_DESC":                        row["ITEM_DESC"],
            "BRAND_RAW":                        row["BRAND_RAW"],
            "PACK_RAW":                         row["PACK_RAW"],
            "UPDATE_REQUIRED":                  1,
            "ASSORTMENT_CATEGORY_DEFINITION":   "AMMO",
            "DEMAND_GROUP":                     "AMMO",
            "DESCRIPTION":                      row["ITEM_DESC"],
            "UPC10":                            f"{1000000000 + idx:010d}",
            "SKU":                              f"SKU{idx:05d}",
            "RAW_BRAND":                        row["BRAND_RAW"].upper(),
            "RAW_MANUFACTURER":                 row["BRAND_RAW"].upper() + " MFR",
            "RAW_ASSORTMENT_CATEGORY":          "AMMO",
            "RAW_TOTAL_DOLLARS":                100.0 + idx,
            "RAW_US_MULTI_RETAILER_RESTRICTED": "",
            "BRAND":                            row["BRAND_RAW"].upper(),
            "TOOL_BRAND":                       row["BRAND_RAW"].upper(),
        }
        for idx, row in enumerate(NEW_PRODUCTS)
    ])


def write_phase1_inputs(workdir: Path) -> tuple[Path, Path]:
    """
    Write the Excel + CSV pair into ``workdir`` and return their paths.

    Used by both the integration test and any future smoke-test that
    wants a small, fast Phase 1 input pair.
    """
    xlsx_path = workdir / "fixture.xlsx"
    csv_path  = workdir / "fixture.csv"

    with pd.ExcelWriter(xlsx_path, engine="xlsxwriter") as writer:
        build_meta_df().to_excel(writer, sheet_name="META", index=False)
        build_history_df().to_excel(writer, sheet_name="FINAL", index=False)

    build_flat_file_df().to_csv(csv_path, index=False)
    return xlsx_path, csv_path


def write_malformed_phase1_xlsx(
    workdir: Path,
    *,
    drop_meta: bool = False,
    drop_final: bool = False,
    drop_meta_column: str | None = None,
    name: str = "fixture.xlsx",
) -> Path:
    """
    Build an xlsx that fails one of the validation guards in run_phase1.

    Mirrors the three RuntimeError paths in 1_Phase_1_Attribute_Mapping.py:
    'No META sheet', 'No FINAL sheet', 'META sheet missing column …'.
    """
    path = workdir / name
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        if not drop_meta:
            meta = build_meta_df()
            if drop_meta_column and drop_meta_column in meta.columns:
                meta = meta.drop(columns=[drop_meta_column])
            meta.to_excel(writer, sheet_name="META", index=False)
        if not drop_final:
            build_history_df().to_excel(writer, sheet_name="FINAL", index=False)
        # xlsxwriter refuses to write an empty workbook so always include
        # at least one sheet — a synthetic placeholder when both are dropped.
        if drop_meta and drop_final:
            pd.DataFrame({"placeholder": [1]}).to_excel(
                writer, sheet_name="OTHER", index=False,
            )
    return path


def write_phase1_zip(
    workdir: Path,
    *,
    with_wrapper_folder: bool = False,
    omit_xlsx: bool = False,
    omit_csv: bool = False,
    extra_txt_files: bool = False,
    name: str = "fixture.zip",
) -> Path:
    """
    Build a Phase 1 zip from the synthetic fixture.

    ``with_wrapper_folder`` mirrors how analysts package their projects
    (single top-level directory inside the zip).  ``extra_txt_files``
    adds ModelInfo.txt / Attributes.txt / AttributeValues.txt so the
    same zip can be reused for a Phase 2 run after Phase 1 finishes.
    """
    zip_path = workdir / name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        prefix = "project/" if with_wrapper_folder else ""

        if not omit_xlsx:
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
                build_meta_df().to_excel(writer, sheet_name="META", index=False)
                build_history_df().to_excel(writer, sheet_name="FINAL", index=False)
            zf.writestr(f"{prefix}fixture.xlsx", buf.getvalue())

        if not omit_csv:
            zf.writestr(f"{prefix}fixture.csv",
                        build_flat_file_df().to_csv(index=False))

        if extra_txt_files:
            zf.writestr(f"{prefix}ModelInfo.txt",      _model_info_txt())
            zf.writestr(f"{prefix}Attributes.txt",     _attributes_txt())
            zf.writestr(f"{prefix}AttributeValues.txt", _attribute_values_txt())

    return zip_path


# ── Phase 2 fixtures ─────────────────────────────────────────────────────────
# Phase 2 starts from the Phase 1 output workbook (File_For_Mapping_QC.xlsx)
# plus three text files describing the model schema.  The phase3_package
# pipeline reads the FLAT_FILE sheet and the txt files; here we generate a
# minimal-but-realistic shape for both.

PHASE2_BRANDS = ["ACME", "ZETA", "OMEGA", "PRIVATE LABEL ACME"]
PHASE2_MISMATCH_PAIRS = [
    # (raw_brand, tool_brand) — second pair is a deliberate mismatch.
    ("ACME",                 "ACME"),
    ("ZETA",                 "ZETA"),
    ("OMEGA",                "ACME"),                    # genuine mismatch
    ("PRIVATE LABEL ACME",   "PRIVATE LABEL RESTRICTED"),# expected
]


def build_phase2_flat_file_df(rows_per_pair: int = 3) -> pd.DataFrame:
    """
    Minimal FLAT_FILE sheet — covers the columns scanned by phase3_package
    (RAW_*, BRAND, TOOL_BRAND, plus a DESCRIPTION + RMRR column so the
    mismatch enrichment has something to render).
    """
    rows: list[dict] = []
    item_id = 1
    for brand_raw, tool_brand in PHASE2_MISMATCH_PAIRS:
        for n in range(rows_per_pair):
            rows.append({
                "ITEM_DIM_KEY":                       item_id,
                "DESCRIPTION":                        f"{brand_raw} item {n}",
                "RAW_BRAND":                          brand_raw,
                "RAW_MANUFACTURER":                   brand_raw + " MFR",
                "RAW_ASSORTMENT_CATEGORY":            "AMMO",
                "RAW_TOTAL_DOLLARS":                  100.0 + n,
                "RAW_US_MULTI_RETAILER_RESTRICTED":   "Y" if "OMEGA" in brand_raw else "",
                "BRAND":                              brand_raw,
                "TOOL_BRAND":                         tool_brand,
            })
            item_id += 1
    return pd.DataFrame(rows)


def write_phase2_qc_xlsx(workdir: Path, *, name: str = "File_For_Mapping_QC.xlsx") -> Path:
    """A QC workbook with the FLAT_FILE sheet phase3 reads from."""
    path = workdir / name
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        build_phase2_flat_file_df().to_excel(writer, sheet_name="FLAT_FILE", index=False)
        build_meta_df().to_excel(writer, sheet_name="META", index=False)
    return path


def _model_info_txt() -> str:
    """Pipe-delimited model_info — phase3_package._scan_directory uses '|'."""
    return (
        "Category_Name|ModelName|AssortmentCategory|UPC10Length|FlatFileExt\n"
        "AMMO|BASE|AMMO|10|.csv\n"
    )


def _attributes_txt() -> str:
    return (
        "Attribute_Id|Attribute_Name|Attribute_Type|Attribute_Group\n"
        "1|BRAND|MODELING|BRAND\n"
        "2|PACK_SIZE|MODELING|PACK_SIZE\n"
    )


def _attribute_values_txt() -> str:
    return (
        "Attribute_Id|Attribute_Value\n"
        "1|ACME\n"
        "1|ZETA\n"
        "1|OMEGA\n"
        "1|PRIVATE LABEL ACME\n"
        "2|12 OZ\n"
        "2|24 OZ\n"
    )


def write_phase2_loose_files(workdir: Path) -> dict[str, Path]:
    """Write the four Phase 2 input files into ``workdir``."""
    qc_xlsx_path = write_phase2_qc_xlsx(workdir)
    paths = {
        "xlsx":             qc_xlsx_path,
        "model_info":       workdir / "ModelInfo.txt",
        "attributes":       workdir / "Attributes.txt",
        "attribute_values": workdir / "AttributeValues.txt",
    }
    paths["model_info"].write_text(_model_info_txt())
    paths["attributes"].write_text(_attributes_txt())
    paths["attribute_values"].write_text(_attribute_values_txt())
    return paths


def write_phase2_zip(
    workdir: Path,
    *,
    with_wrapper_folder: bool = False,
    omit_qc_xlsx: bool = False,
    omit_model_info: bool = False,
    omit_attribute_values: bool = False,
    name: str = "phase2.zip",
) -> Path:
    """Build a Phase 2 zip in the shape phase3_package._scan_directory expects."""
    zip_path = workdir / name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        prefix = "project/" if with_wrapper_folder else ""

        if not omit_qc_xlsx:
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
                build_phase2_flat_file_df().to_excel(writer, sheet_name="FLAT_FILE", index=False)
                build_meta_df().to_excel(writer, sheet_name="META", index=False)
            zf.writestr(f"{prefix}File_For_Mapping_QC.xlsx", buf.getvalue())

        if not omit_model_info:
            zf.writestr(f"{prefix}ModelInfo.txt", _model_info_txt())
        zf.writestr(f"{prefix}Attributes.txt",      _attributes_txt())
        if not omit_attribute_values:
            zf.writestr(f"{prefix}AttributeValues.txt", _attribute_values_txt())

    return zip_path


def write_post_qc_xlsx(workdir: Path, *, name: str = "output_edited.xlsx") -> Path:
    """
    A post-QC re-upload — the Cleaned Output sheet from a Phase 2 run with
    one row's BRAND edited so the post-QC re-collapse has work to do.
    """
    flat = build_phase2_flat_file_df()
    flat.loc[2, "BRAND"] = "ACME"   # was OMEGA — analyst correction
    path = workdir / name
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        flat.to_excel(writer, sheet_name="Cleaned Output", index=False)
    return path
