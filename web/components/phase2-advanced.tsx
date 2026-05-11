"use client";

// Advanced Phase 2 configuration disclosure: private-label rules (per-retailer
// enable + label + parent column) and Client Brand override rules. The
// brand/tool_brand pair itself is resolved from Attributes.txt at run time,
// so the only column-picker here is the manufacturer column.

import type { BrandOverrideConfig, Phase2ScanResult, PrivateLabelRule } from "@/lib/types";

export type PrivateLabelRules = Record<string, PrivateLabelRule>;

// Row-shaped override rule; one row → one single-element BrandOverrideRule on submit.
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

  brandOverride: BrandOverrideConfig;
  setBrandOverride: (next: BrandOverrideConfig) => void;

  brandOverrideRows: BrandOverrideRow[];
  setBrandOverrideRows: (next: BrandOverrideRow[]) => void;
}) {
  // Manufacturer values track whichever column the analyst picked.  Brand /
  // TOOL_BRAND values come from the scan's union across every column the
  // Attributes.txt brand-pair resolver returned (handles SUB_BRAND clients
  // and multi-model BRAND_MULO/BRAND_DRUG/etc. without analyst input).
  const colValues = scan?.column_values ?? {};
  const mfrValues =
    colValues[brandOverride.raw_manufacturer_col] ?? scan?.manufacturer_values ?? [];
  const brandValues = scan?.brand_values ?? [];
  const toolBrandValues = scan?.tool_brand_values ?? [];
  const detectedPairs = scan?.detected_brand_pairs ?? [];

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

          {/* Parent column — drives PL retailer detection (Step 5) and the
              PARENT column rendered in the BRAND-vs-TOOL_BRAND mismatch
              dialog (Step 13).  Lives in the Private Label section because
              that's where its effect shows up; kept separate from the
              brand-override rule editor so swapping it doesn't surprise
              the analyst into losing PL/CVS visibility. */}
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

          {/* Detected brand pair(s) — sourced from each Attributes.txt's
              Brand_Attribute=Y row.  Surfaces what the pipeline will use
              for both PL retagging and override rules so analysts can
              spot a misconfigured Attributes.txt before the run starts. */}
          {detectedPairs.length > 0 && (
            <div className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-[11px] text-emerald-900">
              <span className="font-medium">Detected brand pair(s) from Attributes.txt:</span>{" "}
              {detectedPairs
                .map((p) => `${p.brand_col} / ${p.tool_brand_col}`)
                .join("; ")}
            </div>
          )}
          {scan && detectedPairs.length === 0 && (
            <div className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-900">
              No <code>Brand_Attribute=Y</code> row found in any Attributes.txt — falling back
              to literal <code>BRAND</code> / <code>TOOL_BRAND</code> columns at run time.
            </div>
          )}

          {/* Manufacturer column — the only column-picker left in this
              section.  BRAND and TOOL_BRAND columns are resolved from
              Attributes.txt now (see status row above). */}
          <label className="block text-xs text-zinc-600 max-w-md">
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

          {/* Row-shaped rules editor — mirrors Streamlit's data_editor.
              Each row maps to a single-element BrandOverrideRule on
              submission.  3-column grid (manufacturer / from-BRAND /
              to-TOOL_BRAND) plus a fixed-width row-delete slot. */}
          <div className="space-y-2">
            <div className="grid grid-cols-1 md:grid-cols-[1fr_1fr_1fr_2.5rem] gap-3 text-[11px] text-zinc-500 font-medium">
              <div>Manufacturer value</div>
              <div>From BRAND value</div>
              <div>To TOOL_BRAND value</div>
              <div />
            </div>

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
                  Reference: existing TOOL_BRAND values ({toolBrandValues.length})
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


// Select dropdown when known options exist; falls back to plain input.
// The "(custom)" sentinel preserves a value that isn't in the list.
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
