"use client";

import type { BrandOverrideConfig, Phase2ScanResult, PrivateLabelRule } from "@/lib/types";

/**
 * Advanced Phase 2 configuration disclosure.
 *
 * Mirrors the two data editors on pages/2_Phase_3_Pipeline_and_QC.py:
 *   • Private Label Rules — per-retailer (walmart/cvs/heb) enable + label
 *   • Brand Override Rules — toggle + rules table + dependent column names
 *
 * All four pieces stay collapsed by default because the existing
 * walmart/cvs/heb defaults match the v1 production pipeline.  Most
 * analysts only touch the panel when running a custom retailer or a
 * manufacturer override — exposing the editors makes that possible
 * without a hand-edit of the JSON config.
 */

export type PrivateLabelRules = Record<string, PrivateLabelRule>;

/**
 * Row-shaped brand override rule, matching what the Streamlit
 * st.data_editor produced.  Each row maps to a single-element
 * BrandOverrideRule on submission.
 */
export type BrandOverrideRow = {
  manufacturer:  string;
  from_brand:    string;
  to_tool_brand: string;
};

export function Phase2AdvancedConfig({
  expanded,
  onToggle,
  scan,

  privateLabelRules,
  setPrivateLabelRules,
  plBaseName,
  setPlBaseName,

  brandOverride,
  setBrandOverride,

  brandOverrideRows,
  setBrandOverrideRows,
}: {
  expanded: boolean;
  onToggle: () => void;
  scan: Phase2ScanResult | null;

  privateLabelRules: PrivateLabelRules;
  setPrivateLabelRules: (next: PrivateLabelRules) => void;
  plBaseName: string;
  setPlBaseName: (next: string) => void;

  brandOverride: BrandOverrideConfig;
  setBrandOverride: (next: BrandOverrideConfig) => void;

  brandOverrideRows: BrandOverrideRow[];
  setBrandOverrideRows: (next: BrandOverrideRow[]) => void;
}) {
  // Source the rule-editor dropdowns from whichever column the analyst
  // picked in the column-name fields above.  Fall back to the static
  // *_values fields the scan returns for the defaults so the dropdowns
  // still have content when scan finishes before the user changes the
  // column pickers.
  const colValues = scan?.column_values ?? {};
  const mfrValues =
    colValues[brandOverride.raw_manufacturer_col] ?? scan?.manufacturer_values ?? [];
  const brandValues =
    colValues[brandOverride.brand_col] ?? scan?.brand_values ?? [];
  const toolBrandValues =
    colValues[brandOverride.tool_brand_col] ?? scan?.tool_brand_values ?? [];

  return (
    <details
      open={expanded}
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open !== expanded) onToggle();
      }}
      className="rounded-xl border border-zinc-200 bg-white"
    >
      <summary className="cursor-pointer select-none px-4 py-3 text-sm font-medium text-zinc-700 hover:bg-zinc-50 rounded-xl">
        Project Scope Configuration
      </summary>

      <div className="px-4 pb-4 space-y-10 border-t border-zinc-100 pt-4">
        {/* ── Private Label Rules ─────────────────────────────────────── */}
        <section className="space-y-2">
          <div className="text-sm font-semibold text-brand-700">Private label rules</div>
          <p className="text-xs text-zinc-500">
            For each retailer, choose whether private label restricted is enabled
            and what the label should be.
          </p>
          <table className="w-full text-sm">
            <thead className="bg-zinc-50 text-zinc-600 text-xs uppercase tracking-wide">
              <tr>
                <th className="px-3 py-2 text-left">Retailer</th>
                <th className="px-3 py-2 text-left">Enabled</th>
                <th className="px-3 py-2 text-left">Label</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(privateLabelRules).map(([retailer, rule]) => (
                <tr key={retailer} className="border-t border-zinc-100">
                  <td className="px-3 py-2 text-xs">{retailer}</td>
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      checked={rule.enabled}
                      onChange={(e) =>
                        setPrivateLabelRules({
                          ...privateLabelRules,
                          [retailer]: { ...rule, enabled: e.target.checked },
                        })
                      }
                    />
                  </td>
                  <td className="px-3 py-2">
                    <input
                      type="text"
                      value={rule.label}
                      disabled={!rule.enabled}
                      onChange={(e) =>
                        setPrivateLabelRules({
                          ...privateLabelRules,
                          [retailer]: { ...rule, label: e.target.value },
                        })
                      }
                      className="border border-zinc-300 rounded px-2 py-1 text-xs w-full disabled:bg-zinc-100"
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <label className="block text-xs text-zinc-600">
            <span className="block mb-1">Private-label target attribute (default: TOOL_BRAND)</span>
            <ColumnSelect
              value={plBaseName}
              onChange={setPlBaseName}
              options={scan?.all_columns ?? []}
              emptyLabel="(blank = use TOOL_BRAND)"
              fallbackPlaceholder="(blank = use TOOL_BRAND)"
            />
            <span className="block mt-1 text-[11px] text-zinc-500">
              Leave blank for standard projects. Pick a different attribute (e.g. SUBBRAND)
              for multi-model projects where PL retagging applies to TOOL_SUBBRAND_*
              variants instead of TOOL_BRAND.
            </span>
          </label>

          {/* Parent column — drives PL retailer detection (Step 5) and the
              PARENT column rendered in the BRAND-vs-TOOL_BRAND mismatch
              dialog (Step 13).  Lives in the Private Label section because
              that's where its effect shows up; kept separate from the
              brand-override rule editor's column pickers so swapping it
              doesn't surprise the analyst into losing PL/CVS visibility. */}
          <label className="block text-xs text-zinc-600">
            <span className="block mb-1">Parent column (PL detection + mismatch dialog)</span>
            {scan && scan.raw_parent_columns.length > 0 ? (
              <select
                value={brandOverride.raw_parent_col}
                onChange={(e) =>
                  setBrandOverride({ ...brandOverride, raw_parent_col: e.target.value })
                }
                className="border border-zinc-300 rounded px-2 py-1 text-xs w-full"
              >
                {scan.raw_parent_columns.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={brandOverride.raw_parent_col}
                onChange={(e) =>
                  setBrandOverride({ ...brandOverride, raw_parent_col: e.target.value })
                }
                className="border border-zinc-300 rounded px-2 py-1 text-xs w-full"
              />
            )}
            <span className="block mt-1 text-[11px] text-zinc-500">
              Pick the column with retailer/parent values (e.g. RAW_PARENT) so private-label
              retailers like CVS surface in the mismatch dialog.
            </span>
          </label>
        </section>

        {/* ── Brand Override ───────────────────────────────────────────
            Always shown — analysts run this every project, so gating it
            behind an opt-in checkbox just hides the column pickers and
            forces an extra click.  An empty rules table is a no-op for
            the pipeline (transforms.apply_brand_overrides skips when the
            list is empty), so always-on is safe. */}
        <section className="space-y-3 border-t border-zinc-100 pt-8">
          <div>
            <div className="text-sm font-semibold text-brand-700">Client Brand rules</div>
            <p className="text-xs text-zinc-500 mt-0.5">
              Force-map (manufacturer, BRAND) pairs to a specific TOOL_BRAND value.
              Each rule rewrites TOOL_BRAND only on rows where the manufacturer column
              matches one of the listed values — leaving every other row untouched.
            </p>
          </div>

          {/* Top column-pickers + bottom rule editor share the same 4-track
              grid template (3 equal data columns + a fixed-width slot for
              the row-delete button).  This makes the rule dropdowns line
              up under the column-name selectors above pixel-for-pixel,
              instead of drifting because of table auto-sizing. */}
          <div className="grid grid-cols-1 md:grid-cols-[1fr_1fr_1fr_2.5rem] gap-3">
            <label className="block text-xs text-zinc-600">
              <span className="block mb-1">Manufacturer column (override rules)</span>
              {scan && scan.raw_manufacturer_columns.length > 0 ? (
                <select
                  value={brandOverride.raw_manufacturer_col}
                  onChange={(e) =>
                    setBrandOverride({ ...brandOverride, raw_manufacturer_col: e.target.value })
                  }
                  className="border border-zinc-300 rounded px-2 py-1 text-xs w-full"
                >
                  {scan.raw_manufacturer_columns.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={brandOverride.raw_manufacturer_col}
                  onChange={(e) =>
                    setBrandOverride({ ...brandOverride, raw_manufacturer_col: e.target.value })
                  }
                  className="border border-zinc-300 rounded px-2 py-1 text-xs w-full"
                />
              )}
              <span className="block mt-1 text-[11px] text-zinc-500">
                Used only to match each rule's manufacturer values — separate from the Parent column above.
              </span>
            </label>
            <label className="block text-xs text-zinc-600">
              <span className="block mb-1">BRAND column</span>
              <ColumnSelect
                value={brandOverride.brand_col}
                onChange={(v) => setBrandOverride({ ...brandOverride, brand_col: v })}
                options={scan?.all_columns ?? []}
                fallbackPlaceholder="BRAND"
              />
            </label>
            <label className="block text-xs text-zinc-600">
              <span className="block mb-1">TOOL_BRAND column</span>
              <ColumnSelect
                value={brandOverride.tool_brand_col}
                onChange={(v) => setBrandOverride({ ...brandOverride, tool_brand_col: v })}
                options={scan?.all_columns ?? []}
                fallbackPlaceholder="TOOL_BRAND"
              />
            </label>
            {/* Empty cell aligning with the row-delete column below. */}
            <div className="hidden md:block" />
          </div>

          {/* Row-shaped rules editor — mirrors Streamlit's data_editor on
              lines 1004-1033.  Each row maps to a single-element
              BrandOverrideRule on submission.  Same grid template as the
              column-pickers above so dropdowns align column-to-column. */}
          <div className="space-y-2">
            {/* Empty grey separator — column labels were redundant with the
                column-picker headers above and the placeholder text in
                each rule field below. */}
            <div className="bg-zinc-50 px-3 py-2 rounded" />

            <div className="space-y-1.5">
              {brandOverrideRows.map((row, i) => (
                <div
                  key={i}
                  className="grid grid-cols-1 md:grid-cols-[1fr_1fr_1fr_2.5rem] gap-3 items-center"
                >
                  <RuleField
                    value={row.manufacturer}
                    options={mfrValues}
                    placeholder="manufacturer value"
                    onChange={(v) =>
                      setBrandOverrideRows(
                        brandOverrideRows.map((r, j) =>
                          j === i ? { ...r, manufacturer: v } : r,
                        ),
                      )
                    }
                  />
                  <RuleField
                    value={row.from_brand}
                    options={brandValues}
                    placeholder="old BRAND value"
                    onChange={(v) =>
                      setBrandOverrideRows(
                        brandOverrideRows.map((r, j) =>
                          j === i ? { ...r, from_brand: v } : r,
                        ),
                      )
                    }
                  />
                  <RuleField
                    value={row.to_tool_brand}
                    options={toolBrandValues}
                    placeholder="new TOOL_BRAND value"
                    onChange={(v) =>
                      setBrandOverrideRows(
                        brandOverrideRows.map((r, j) =>
                          j === i ? { ...r, to_tool_brand: v } : r,
                        ),
                      )
                    }
                  />
                  <button
                    type="button"
                    aria-label="Remove rule"
                    onClick={() => {
                      // Keep at least one empty row visible so the editor
                      // is always primed.  Removing the last row resets
                      // it instead of collapsing the table.
                      const rest = brandOverrideRows.filter((_, j) => j !== i);
                      setBrandOverrideRows(
                        rest.length > 0
                          ? rest
                          : [{ manufacturer: "", from_brand: "", to_tool_brand: "" }],
                      );
                    }}
                    className="justify-self-end text-base leading-none text-zinc-400 hover:text-err px-1"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
            <button
              type="button"
              onClick={() =>
                setBrandOverrideRows([
                  ...brandOverrideRows,
                  { manufacturer: "", from_brand: "", to_tool_brand: "" },
                ])
              }
              className="text-xs text-brand-700 hover:text-brand-900 underline"
            >
              + Add rule
            </button>
            {toolBrandValues.length > 0 && (
              <details className="text-xs text-zinc-500">
                <summary className="cursor-pointer">
                  Reference: existing {brandOverride.tool_brand_col || "TOOL_BRAND"} values ({toolBrandValues.length})
                </summary>
                <div className="mt-1 max-h-32 overflow-y-auto font-mono">
                  {toolBrandValues.join(", ")}
                </div>
              </details>
            )}
          </div>
        </section>
      </div>
    </details>
  );
}


/**
 * Column-name picker.  Renders as a <select> populated from the scan's
 * column list when one is available, falling back to a free-text input
 * before the upload has been scanned.  Preserves any current value not
 * in the option list as a "(custom)" entry so configs from older runs
 * don't lose their column on first render.
 *
 * Used for the BRAND / TOOL_BRAND / PL base name fields.  Backs the
 * "reduce manual error" goal of the Phase 2 advanced config — analysts
 * pick from real columns rather than typing a name that risks a typo.
 */
function ColumnSelect({
  value, onChange, options,
  emptyLabel = "(none)",
  fallbackPlaceholder,
}: {
  value: string;
  onChange: (next: string) => void;
  options: string[];
  emptyLabel?: string;
  fallbackPlaceholder?: string;
}) {
  if (options.length === 0) {
    return (
      <input
        type="text"
        value={value}
        placeholder={fallbackPlaceholder}
        onChange={(e) => onChange(e.target.value)}
        className="border border-zinc-300 rounded px-2 py-1 text-xs w-full"
      />
    );
  }
  const isCustom = value !== "" && !options.includes(value);
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="border border-zinc-300 rounded px-2 py-1 text-xs w-full"
    >
      <option value="">{emptyLabel}</option>
      {isCustom && <option value={value}>{value} (custom)</option>}
      {options.map((c) => <option key={c} value={c}>{c}</option>)}
    </select>
  );
}


/**
 * Free-text input with an optional dropdown of known values, mirroring
 * how Streamlit's data_editor falls back to TextColumn when no values
 * are available.  Renders as a <select> when ``options`` is non-empty,
 * otherwise as a plain <input>.
 */
function RuleField({
  value, options, placeholder, onChange,
}: {
  value: string;
  options: string[] | undefined;
  placeholder: string;
  onChange: (next: string) => void;
}) {
  if (options && options.length > 0) {
    // Allow free-text via a sentinel option ("(custom)") that swaps to a
    // text input — mirrors how Streamlit Selectbox columns let users
    // type a value that's not in the list.
    const isCustom = value !== "" && !options.includes(value);
    return (
      <select
        value={isCustom ? "__CUSTOM__" : value}
        onChange={(e) => onChange(e.target.value === "__CUSTOM__" ? value : e.target.value)}
        className="border border-zinc-300 rounded px-2 py-1 text-xs w-full"
      >
        <option value="">{placeholder}</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
        {isCustom && <option value="__CUSTOM__">(custom: {value})</option>}
      </select>
    );
  }
  return (
    <input
      type="text"
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className="border border-zinc-300 rounded px-2 py-1 text-xs w-full"
    />
  );
}
