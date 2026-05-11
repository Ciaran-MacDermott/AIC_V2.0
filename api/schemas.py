"""
Request/response shapes for the AIC API.

Mirrored manually in web/lib/types.ts. When you add or change a model
here, update the TS types in the same commit.
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel

JobState = Literal[
    "queued",
    "running",
    "qc_ready",
    "finalizing",
    "done",
    "error",
    "stopped",
    # Phase 2/3 only
    "mismatch_pending",
    "post_qc_running",
    "post_qc_done",
]

Phase = Literal["phase1", "phase2"]


class RunCreated(BaseModel):
    run_id: str


class JobStatus(BaseModel):
    run_id: str
    phase: Phase
    state: JobState
    progress: float            # 0.0 – 1.0
    stage_label: str
    started_at: float          # epoch seconds
    elapsed_s: float
    error: Optional[str] = None
    error_title:    Optional[str] = None
    error_advice:   Optional[str] = None
    error_category: Optional[str] = None    # "input" | "config" | "server"
    qc_sheet_keys: Optional[list[str]] = None
    mismatch_count: Optional[int] = None
    post_qc_categories: Optional[list[str]] = None
    parent_run_id: Optional[str] = None
    log_cursor: int            # total lines emitted so far
    log_tail: list[str]        # last ~60 lines for at-a-glance UI
    # Concurrent-user UX: only populated while state == "queued".
    # queue_position is 0-indexed against runs ahead (running + queued earlier).
    queue_position: Optional[int] = None
    queue_depth:    Optional[int] = None
    eta_seconds:    Optional[float] = None


class LogChunk(BaseModel):
    cursor: int                # next `since` value to pass back
    lines: list[str]


class ActiveRunSummary(BaseModel):
    """Compact snapshot of a live run for the dashboard widget."""
    run_id:         str
    phase:          Phase
    state:          JobState
    stage_label:    str
    progress:       float
    started_at:     float
    elapsed_s:      float
    parent_run_id:  Optional[str] = None


class ActiveRuns(BaseModel):
    runs: list[ActiveRunSummary]


class ColumnDef(BaseModel):
    field: str
    header: str
    editable: bool = False
    type: Literal["text", "number"] = "text"


class QcSheetSummary(BaseModel):
    key: str
    label: str                 # the attribute name (e.g. "BRAND")
    row_count: int
    edited_count: int


class QcSheetList(BaseModel):
    sheets: list[QcSheetSummary]


class QcSheetPayload(BaseModel):
    key: str
    attribute: str
    columns: list[ColumnDef]
    rows: list[dict[str, Any]]            # each row carries a stable "_row_id"
    attribute_options: list[str]          # dropdown values
    original_values: dict[str, str]       # row_id -> original attribute value
    row_flags: dict[str, list[str]]       # row_id -> flag tokens


class QcEditedRow(BaseModel):
    row_id: str
    attribute_value: str


class QcEditPayload(BaseModel):
    edited_rows: list[QcEditedRow]


class QcFinalized(BaseModel):
    download_url: str


# ── Phase 2 ──────────────────────────────────────────────────────────────────

class PrivateLabelRule(BaseModel):
    enabled: bool = True
    label:   str  = "PRIVATE LABEL RESTRICTED"


class BrandOverrideRule(BaseModel):
    manufacturers:   list[str]
    brand_overrides: dict[str, str]


class BrandOverrideConfig(BaseModel):
    enable:               bool = False
    # Drives manufacturer matching for the brand-override cleanup steps
    # (10.6 / 11) in phase3_package/transforms.py.
    raw_manufacturer_col: str  = "RAW_MANUFACTURER"
    # Drives Step 5 private-label retailer detection (apply_private_label_rules)
    # AND the PARENT column rendered in the BRAND-vs-TOOL_BRAND mismatch
    # dialog (Step 13).  In current Circana data the retailer-shaped values
    # live in RAW_MANUFACTURER; RAW_PARENT is kept as a fallback for older
    # project shapes.  Split from raw_manufacturer_col so analysts who pick
    # a different column for one role don't disturb the other.
    raw_parent_col:       str  = "RAW_MANUFACTURER"
    rules:                list[BrandOverrideRule] = []
    # Note: brand_col / tool_brand_col were dropped — the brand pair is
    # resolved per-model at runtime from each Attributes.txt's
    # Brand_Attribute=Y row (see phase3_package.pipeline._resolve_brand_pairs).


class Phase2Config(BaseModel):
    """
    Configuration for a Phase 2 run.  Mirrors the Streamlit page's data
    editor + selectbox controls so the new UI can post the same shape.
    """
    raw_upc_pl_brand_col:  str
    private_label_config:  dict[str, PrivateLabelRule] = {
        "walmart": PrivateLabelRule(enabled=True,  label="PRIVATE LABEL RESTRICTED"),
        "cvs":     PrivateLabelRule(enabled=True,  label="PRIVATE LABEL EXCLUDE"),
        "heb":     PrivateLabelRule(enabled=False, label="PRIVATE LABEL RESTRICTED"),
    }
    brand_override_config: BrandOverrideConfig = BrandOverrideConfig()
    is_custom_collapse:    bool = False
    skip_rmrr:             bool = False


class MismatchRow(BaseModel):
    """One distinct (BRAND, TOOL_BRAND[, PARENT]) pair that needs review."""
    BRAND:      str
    TOOL_BRAND: str
    PARENT:     Optional[str] = None


class MismatchGroup(BaseModel):
    model_suffix:   str = ""
    brand_col:      str
    tool_brand_col: str
    parent_col:     Optional[str] = None
    rows:           list[dict[str, str]]


class MismatchPayload(BaseModel):
    groups: list[MismatchGroup]
    # Dropdown options sourced from the full pipeline df so the wizard can
    # offer the analyst every legitimate value, not just the ones inside
    # the mismatch group.  Mirrors lines 1305-1314 of the Streamlit page.
    brand_values:      list[str] = []
    tool_brand_values: list[str] = []


class MismatchCorrection(BaseModel):
    """
    Analyst's resolution for a single mismatched pair.  Mirrors the dict
    shape consumed by phase3_package.pipeline.run_from_step_14.
    """
    type:           Literal["brand", "tool_brand"]
    brand:          str
    tool_brand_old: str
    brand_new:      str = ""
    tool_brand_new: str = ""
    parent:         str = ""
    brand_col:      str
    tool_brand_col: str


class MismatchResolve(BaseModel):
    corrections: list[MismatchCorrection]


class Phase2Done(BaseModel):
    download_url: str


class Phase2ScanResult(BaseModel):
    """
    Autodetected column metadata for a freshly-uploaded Phase 2 input.

    Mirrors the seven values stashed in st.session_state.p3_* by
    _load_cols_from_dir / _load_cols_from_bytes so the new UI can
    pre-populate its dropdowns without asking the user to type column
    names by hand.
    """
    scan_id:                   str
    raw_upc_columns:           list[str]
    raw_manufacturer_columns:  list[str]
    raw_parent_columns:        list[str]
    all_columns:               list[str]
    default_upc_col:           str
    default_manufacturer_col:  str
    default_parent_col:        str
    manufacturer_values:       list[str]
    # Distinct values across every brand / tool_brand column resolved from
    # each Attributes.txt's Brand_Attribute=Y row (handles single-model
    # projects that only have BRAND/TOOL_BRAND, multi-model projects that
    # have BRAND_MULO/TOOL_BRAND_MULO/etc., and clients whose brand
    # attribute is SUB_BRAND or some other custom name).  Empty list when
    # the project has no Brand_Attribute column (legacy data).
    brand_values:              list[str]
    tool_brand_values:         list[str]
    # The literal column-name pairs the scan resolved.  Surfaced so the UI
    # can show analysts which brand pair(s) were detected without having
    # to ask them to type column names.
    detected_brand_pairs:      list[dict[str, str]] = []
    # Per-column distinct values so the brand-override rule editor can
    # source its dropdowns from whichever column the analyst picked in
    # the column-name fields, not just the defaults.
    column_values:             dict[str, list[str]] = {}


# ── Post-QC re-upload (Phase 3 finalize → category CSVs) ────────────────────

class PostQcDone(BaseModel):
    """
    Result of the post-QC re-upload step: analyst-edited xlsx is
    re-collapsed and split into per-category CSVs, bundled into a zip.
    """
    download_url: str
    categories:   list[str]
