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
  return (
    <details
      open={expanded}
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open !== expanded) onToggle();
      }}
      className="rounded-xl border border-zinc-200 bg-white"
    >
      <summary className="cursor-pointer select-none px-4 py-3 text-sm font-medium text-zinc-700 hover:bg-zinc-50 rounded-xl">
        Advanced configuration
        <span className="ml-2 text-xs text-zinc-500">
          (private-label rules, brand overrides — defaults usually fine)
        </span>
      </summary>

      <div className="px-4 pb-4 space-y-6 border-t border-zinc-100 pt-4">
        {/* ── Private Label Rules ─────────────────────────────────────── */}
        <section className="space-y-2">
          <div className="text-sm font-medium text-zinc-700">Private label rules</div>
          <p className="text-xs text-zinc-500">
            For each retailer, choose whether private-label SKUs get rewritten
            and what label they receive. Streamlit defaults: walmart restricted,
            cvs exclude, heb off.
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
                  <td className="px-3 py-2 font-mono text-xs">{retailer}</td>
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
            <span className="block mb-1">PL base name override</span>
            <input
              type="text"
              value={plBaseName}
              onChange={(e) => setPlBaseName(e.target.value)}
              placeholder="(blank = use TOOL_BRAND)"
              className="border border-zinc-300 rounded px-2 py-1 text-xs w-full"
            />
          </label>
        </section>

        {/* ── Brand Override ───────────────────────────────────────────
            Always shown — analysts run this every project, so gating it
            behind an opt-in checkbox just hides the column pickers and
            forces an extra click.  An empty rules table is a no-op for
            the pipeline (transforms.apply_brand_overrides skips when the
            list is empty), so always-on is safe. */}
        <section className="space-y-3">
          <div>
            <div className="text-sm font-medium text-zinc-800">Brand override rules</div>
            <p className="text-xs text-zinc-500 mt-0.5">
              Force-map specific (manufacturer, brand) pairs to a different
              TOOL_BRAND. Leave the table empty if you don&apos;t need any
              overrides on this run.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <label className="block text-xs text-zinc-600">
              <span className="block mb-1">RAW manufacturer column</span>
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
            </label>
            <label className="block text-xs text-zinc-600">
              <span className="block mb-1">BRAND column</span>
              <input
                type="text"
                value={brandOverride.brand_col}
                onChange={(e) =>
                  setBrandOverride({ ...brandOverride, brand_col: e.target.value })
                }
                className="border border-zinc-300 rounded px-2 py-1 text-xs w-full"
              />
            </label>
            <label className="block text-xs text-zinc-600">
              <span className="block mb-1">TOOL_BRAND column</span>
              <input
                type="text"
                value={brandOverride.tool_brand_col}
                onChange={(e) =>
                  setBrandOverride({ ...brandOverride, tool_brand_col: e.target.value })
                }
                className="border border-zinc-300 rounded px-2 py-1 text-xs w-full"
              />
            </label>
          </div>

          {/* Row-shaped rules editor — mirrors Streamlit's data_editor on
              lines 1004-1033.  Each row maps to a single-element
              BrandOverrideRule on submission. */}
          <div className="space-y-2">
            <table className="w-full text-sm">
              <thead className="bg-zinc-50 text-zinc-600 text-xs uppercase tracking-wide">
                <tr>
                  <th className="px-3 py-2 text-left">Manufacturer</th>
                  <th className="px-3 py-2 text-left">From BRAND</th>
                  <th className="px-3 py-2 text-left">To TOOL_BRAND</th>
                  <th className="w-12" />
                </tr>
              </thead>
              <tbody>
                {brandOverrideRows.map((row, i) => (
                  <tr key={i} className="border-t border-zinc-100">
                    <td className="px-3 py-1.5">
                      <RuleField
                        value={row.manufacturer}
                        options={scan?.manufacturer_values}
                        placeholder="manufacturer"
                        onChange={(v) =>
                          setBrandOverrideRows(
                            brandOverrideRows.map((r, j) =>
                              j === i ? { ...r, manufacturer: v } : r,
                            ),
                          )
                        }
                      />
                    </td>
                    <td className="px-3 py-1.5">
                      <RuleField
                        value={row.from_brand}
                        options={scan?.brand_values}
                        placeholder="old BRAND"
                        onChange={(v) =>
                          setBrandOverrideRows(
                            brandOverrideRows.map((r, j) =>
                              j === i ? { ...r, from_brand: v } : r,
                            ),
                          )
                        }
                      />
                    </td>
                    <td className="px-3 py-1.5">
                      <RuleField
                        value={row.to_tool_brand}
                        options={scan?.tool_brand_values}
                        placeholder="new TOOL_BRAND"
                        onChange={(v) =>
                          setBrandOverrideRows(
                            brandOverrideRows.map((r, j) =>
                              j === i ? { ...r, to_tool_brand: v } : r,
                            ),
                          )
                        }
                      />
                    </td>
                    <td className="px-3 py-1.5 text-right">
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
                        className="text-xs text-zinc-400 hover:text-err"
                      >
                        ×
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
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
            {scan?.tool_brand_values && scan.tool_brand_values.length > 0 && (
              <details className="text-xs text-zinc-500">
                <summary className="cursor-pointer">
                  Reference: existing TOOL_BRAND values ({scan.tool_brand_values.length})
                </summary>
                <div className="mt-1 max-h-32 overflow-y-auto font-mono">
                  {scan.tool_brand_values.join(", ")}
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
