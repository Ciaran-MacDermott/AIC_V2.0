// Mirrors api/schemas.py — keep these in sync when the contract changes.

export type Phase = "phase1" | "phase2";

export type JobState =
  | "queued"
  | "running"
  | "qc_ready"
  | "finalizing"
  | "done"
  | "error"
  | "stopped"
  | "mismatch_pending"
  | "post_qc_running"
  | "post_qc_done";

export type JobStatus = {
  run_id: string;
  phase: Phase;
  state: JobState;
  progress: number;
  stage_label: string;
  started_at: number;
  elapsed_s: number;
  error?: string;
  error_title?: string;
  error_advice?: string;
  error_category?: "input" | "config" | "server";
  qc_sheet_keys?: string[];
  mismatch_count?: number;
  post_qc_categories?: string[];
  parent_run_id?: string;
  log_cursor: number;
  log_tail: string[];
  // Concurrent-user UX — only populated while state === "queued".
  queue_position?: number | null;
  queue_depth?: number | null;
  eta_seconds?: number | null;
};

export type LogChunk = {
  cursor: number;
  lines: string[];
};

export type ActiveRunSummary = {
  run_id: string;
  phase: Phase;
  state: JobState;
  stage_label: string;
  progress: number;
  started_at: number;
  elapsed_s: number;
  parent_run_id?: string | null;
};

export type ActiveRuns = {
  runs: ActiveRunSummary[];
};

export type ColumnDef = {
  field: string;
  header: string;
  editable: boolean;
  type: "text" | "number";
};

export type QcSheetSummary = {
  key: string;
  label: string;
  row_count: number;
  edited_count: number;
};

export type QcSheetList = {
  sheets: QcSheetSummary[];
};

export type QcSheetPayload = {
  key: string;
  attribute: string;
  columns: ColumnDef[];
  rows: Record<string, unknown>[];
  attribute_options: string[];
  original_values: Record<string, string>;
  row_flags: Record<string, string[]>;
};

export type QcEditedRow = {
  row_id: string;
  attribute_value: string;
};

export type QcEditPayload = {
  edited_rows: QcEditedRow[];
};

export type QcFinalized = {
  download_url: string;
};

export type RunCreated = {
  run_id: string;
};


// ── Phase 2 ────────────────────────────────────────────────────────────────

export type PrivateLabelRule = {
  enabled: boolean;
  label: string;
};

export type BrandOverrideConfig = {
  enable: boolean;
  raw_manufacturer_col: string;
  raw_parent_col: string;
  rules: { manufacturers: string[]; brand_overrides: Record<string, string> }[];
};

export type Phase2Config = {
  raw_upc_pl_brand_col: string;
  private_label_config: Record<string, PrivateLabelRule>;
  brand_override_config: BrandOverrideConfig;
  is_custom_collapse: boolean;
  skip_rmrr: boolean;
};

export type MismatchGroup = {
  model_suffix: string;
  brand_col: string;
  tool_brand_col: string;
  parent_col?: string | null;
  rows: Record<string, string>[];
};

export type MismatchPayload = {
  groups: MismatchGroup[];
  brand_values: string[];
  tool_brand_values: string[];
};

export type MismatchCorrection = {
  type: "brand" | "tool_brand";
  brand: string;
  tool_brand_old: string;
  brand_new?: string;
  tool_brand_new?: string;
  parent?: string;
  brand_col: string;
  tool_brand_col: string;
};

export type MismatchResolve = {
  corrections: MismatchCorrection[];
};

export type Phase2Done = {
  download_url: string;
};

export type Phase2ScanResult = {
  scan_id: string;
  raw_upc_columns: string[];
  raw_manufacturer_columns: string[];
  raw_parent_columns: string[];
  all_columns: string[];
  default_upc_col: string;
  default_manufacturer_col: string;
  default_parent_col: string;
  // Default-column values, kept for backward compat — the brand-override
  // rule editor now sources from `column_values` keyed on the active
  // column name so it tracks the user's column-name picks.
  manufacturer_values: string[];
  // Brand / tool_brand values are the union across every column resolved
  // from each Attributes.txt's Brand_Attribute=Y row.  Falls back to the
  // literal "BRAND" / "TOOL_BRAND" columns when no Attributes.txt is
  // available (loose-file uploads, legacy projects).
  brand_values: string[];
  tool_brand_values: string[];
  // Literal (brand_col, tool_brand_col) pairs the scan resolved.  Empty
  // when the project has no Attributes.txt or no Brand_Attribute column.
  detected_brand_pairs?: { brand_col: string; tool_brand_col: string }[];
  column_values?: Record<string, string[]>;
};

export type PostQcDone = {
  download_url: string;
  categories: string[];
};
