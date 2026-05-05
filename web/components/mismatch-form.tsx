"use client";

import { useMemo, useState } from "react";
import type { MismatchCorrection, MismatchGroup } from "@/lib/types";

/**
 * Mismatch review wizard — one group at a time, mirroring the Streamlit
 * page (lines 1276-1382 of pages/2_Phase_3_Pipeline_and_QC.py):
 *
 *   • "Group X of Y" stepper across the top.
 *   • Pre-populated BRAND_NEW / TOOL_BRAND_NEW dropdowns sourced from the
 *     full pipeline df so the analyst sees every legitimate value.
 *   • Greyed-out rows where _is_expected == 1 (PRIVATE LABEL prefix,
 *     RESTRICTED suffix, EXCLUDE in TOOL_BRAND, configured override).
 *   • Light-purple tint on rows the analyst has changed.
 *   • DESCRIPTION + RMRR enrichment columns rendered when the server
 *     attached them.
 *   • "No changes — Continue" / "Next model" / "Continue to Part 2"
 *     navigation pattern.
 *
 * Corrections accumulate across groups and are submitted as a single
 * batch when the analyst exits the last group.
 */

type RowDecision = {
  brand_new:      string;
  tool_brand_new: string;
};

type AccumulatedCorrections = MismatchCorrection[];


export function MismatchForm({
  groups,
  brandValues,
  toolBrandValues,
  onResolve,
  isSubmitting,
}: {
  groups: MismatchGroup[];
  brandValues: string[];
  toolBrandValues: string[];
  onResolve: (corrections: AccumulatedCorrections) => void;
  isSubmitting: boolean;
}) {
  const [groupIdx, setGroupIdx] = useState(0);
  // decisions[`${gi}:${ri}`] = { brand_new, tool_brand_new }
  const [decisions, setDecisions] = useState<Record<string, RowDecision>>({});
  const [accumulated, setAccumulated] = useState<AccumulatedCorrections>([]);

  const total = groups.length;
  const group = groups[groupIdx];
  const isLast = groupIdx === total - 1;

  // Dropdown options — empty option first so analysts can clear, then
  // the unioned brand/tool_brand values from the full pipeline df.
  const brandOptions = useMemo(
    () => ["", ...brandValues],
    [brandValues],
  );
  const toolBrandOptions = useMemo(
    () => ["", ...toolBrandValues],
    [toolBrandValues],
  );

  function rowKey(ri: number): string {
    return `${groupIdx}:${ri}`;
  }

  function decisionFor(ri: number, row: Record<string, string>): RowDecision {
    const stored = decisions[rowKey(ri)];
    if (stored) return stored;
    return {
      brand_new:      row.BRAND ?? "",
      tool_brand_new: row.TOOL_BRAND ?? "",
    };
  }

  function setDecision(ri: number, patch: Partial<RowDecision>): void {
    setDecisions((prev) => {
      const current = prev[rowKey(ri)] ?? {
        brand_new:      group.rows[ri].BRAND ?? "",
        tool_brand_new: group.rows[ri].TOOL_BRAND ?? "",
      };
      return { ...prev, [rowKey(ri)]: { ...current, ...patch } };
    });
  }

  /**
   * Collapse the analyst's edits in this group into the
   * MismatchCorrection[] the server expects.  Mirrors _collect_corrections
   * in the Streamlit page — only emit a correction when the BRAND_NEW or
   * TOOL_BRAND_NEW differs from the original.
   */
  function corrections_for_current_group(): MismatchCorrection[] {
    if (!group) return [];
    const out: MismatchCorrection[] = [];
    group.rows.forEach((row, ri) => {
      const d = decisionFor(ri, row);
      const b_orig  = (row.BRAND ?? "").trim();
      const tb_orig = (row.TOOL_BRAND ?? "").trim();
      const b_new   = d.brand_new.trim();
      const tb_new  = d.tool_brand_new.trim();
      if (b_new === b_orig && tb_new === tb_orig) return;

      const base = {
        brand:          b_orig,
        tool_brand_old: tb_orig,
        parent:         row.PARENT ?? "",
        brand_col:      group.brand_col,
        tool_brand_col: group.tool_brand_col,
      };
      if (b_new !== b_orig) {
        out.push({ ...base, type: "brand", brand_new: b_new });
      }
      if (tb_new !== tb_orig) {
        out.push({ ...base, type: "tool_brand", tool_brand_new: tb_new });
      }
    });
    return out;
  }

  function advanceWith(extraCorrections: MismatchCorrection[]): void {
    const next = [...accumulated, ...extraCorrections];
    if (isLast) {
      onResolve(next);
      return;
    }
    setAccumulated(next);
    setGroupIdx(groupIdx + 1);
  }

  if (!group) {
    return (
      <div className="rounded-lg border border-zinc-200 bg-white p-4 text-sm text-zinc-500">
        No mismatch groups to review.
      </div>
    );
  }

  // Detect optional enrichment columns once per group so we can render
  // them without hardcoding the schema.
  const hasDescription = group.rows.some((r) => r.DESCRIPTION);
  const hasRmrr        = group.rows.some((r) => r.RMRR);

  // Streamlit page rendered DESCRIPTION as just the first 3 words —
  // enough for the analyst to recognise the SKU without bloating column
  // width.  The full text is still on the QC sheet if they need it.
  const firstThreeWords = (s: string): string =>
    s.trim().split(/\s+/).slice(0, 3).join(" ");

  return (
    <section className="space-y-3">
      <div className="rounded-lg border border-amber-200 bg-amber-50 text-amber-900 p-3 text-xs">
        <div className="font-medium text-sm mb-1">
          Brand mismatch review — {group.model_suffix || "base"}{" "}
          ({groupIdx + 1} of {total})
        </div>
        <div>
          Reviewing <code className="font-mono">{group.brand_col}</code> vs{" "}
          <code className="font-mono">{group.tool_brand_col}</code>. Pick the
          intended value from the dropdowns; rows matching an expected pattern
          (PRIVATE LABEL / RESTRICTED / EXCLUDE) are greyed and typically
          require less review.
          {hasDescription && (
            <>
              {" "}The DESCRIPTION column shows the first few words of each
              row's description for context.
            </>
          )}
        </div>
      </div>

      {/* Stepper */}
      <div className="flex items-center gap-2">
        <div className="h-1.5 flex-1 rounded-full bg-brand-100 overflow-hidden">
          <div
            className="h-full bg-brand-600 transition-all duration-300"
            style={{ width: `${((groupIdx + 1) / total) * 100}%` }}
          />
        </div>
        <span className="text-xs tabular-nums text-zinc-500">
          {groupIdx + 1} / {total}
        </span>
      </div>

      {/* Horizontal scroll on overflow — long brand/tool_brand values were
          being truncated when the parent column was narrower than the row.
          whitespace-nowrap on each td keeps column widths sized to content
          so the scroll engages instead of column-squashing. */}
      <div className="surface-card overflow-x-auto overflow-y-auto max-h-[55vh]">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 text-zinc-600 text-xs uppercase tracking-wide">
            <tr>
              <th className="px-3 py-1.5 text-left whitespace-nowrap">{group.brand_col}</th>
              <th className="px-3 py-1.5 text-left whitespace-nowrap">{group.tool_brand_col}</th>
              {group.parent_col && (
                <th className="px-3 py-1.5 text-left whitespace-nowrap">{group.parent_col}</th>
              )}
              {hasDescription && <th className="px-3 py-2 text-left whitespace-nowrap">DESCRIPTION</th>}
              {hasRmrr        && <th className="px-2 py-2 text-left whitespace-nowrap w-12">RMRR</th>}
              <th className="px-3 py-1.5 text-left whitespace-nowrap">BRAND</th>
              <th className="px-3 py-1.5 text-left whitespace-nowrap">TOOL_BRAND</th>
            </tr>
          </thead>
          <tbody>
            {group.rows.map((row, ri) => {
              const isExpected = (row._is_expected ?? "0") === "1";
              const d = decisionFor(ri, row);
              const changed =
                d.brand_new.trim()      !== (row.BRAND ?? "").trim() ||
                d.tool_brand_new.trim() !== (row.TOOL_BRAND ?? "").trim();

              const rowClass = [
                "border-t border-zinc-100",
                changed   ? "bg-[rgba(166,135,183,0.18)]" : "",
                isExpected && !changed ? "text-zinc-400 bg-zinc-100" : "",
              ].join(" ");

              return (
                <tr key={ri} className={rowClass}>
                  <td className="px-3 py-1.5 whitespace-nowrap text-[11px]">{row.BRAND}</td>
                  <td className="px-3 py-1.5 whitespace-nowrap text-[11px]">{row.TOOL_BRAND}</td>
                  {group.parent_col && (
                    <td className="px-3 py-1.5 whitespace-nowrap text-[11px]">{row.PARENT ?? ""}</td>
                  )}
                  {hasDescription && (
                    <td className="px-3 py-1.5 whitespace-nowrap text-[11px]">{firstThreeWords(row.DESCRIPTION ?? "")}</td>
                  )}
                  {hasRmrr && (
                    <td className="px-2 py-1.5 whitespace-nowrap text-[11px] w-12">
                      {row.RMRR === "RES" ? (
                        <span className="rounded bg-amber-100 text-amber-800 px-1.5 py-0.5">
                          RES
                        </span>
                      ) : ""}
                    </td>
                  )}
                  <td className="px-3 py-1.5">
                    <select
                      value={d.brand_new}
                      onChange={(e) => setDecision(ri, { brand_new: e.target.value })}
                      className="border border-zinc-300 rounded px-2 py-1 text-[11px] w-full min-w-[12rem]"
                    >
                      {brandOptions.map((v) => (
                        <option key={v} value={v}>{v || "—"}</option>
                      ))}
                    </select>
                  </td>
                  <td className="px-3 py-1.5">
                    <select
                      value={d.tool_brand_new}
                      onChange={(e) => setDecision(ri, { tool_brand_new: e.target.value })}
                      className="border border-zinc-300 rounded px-2 py-1 text-[11px] w-full min-w-[12rem]"
                    >
                      {toolBrandOptions.map((v) => (
                        <option key={v} value={v}>{v || "—"}</option>
                      ))}
                    </select>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={isSubmitting}
          onClick={() => advanceWith([])}
          className="btn-secondary"
        >
          {isLast ? "No changes — finish" : "No changes — continue"}
        </button>
        <button
          type="button"
          disabled={isSubmitting}
          onClick={() => advanceWith(corrections_for_current_group())}
          className="btn-primary"
        >
          {isSubmitting
            ? "Submitting…"
            : isLast
              ? "Submit & resume Part B"
              : `Save & next  (${groupIdx + 2} of ${total})`}
        </button>
        <span className="text-xs text-zinc-500 ml-auto">
          Greyed rows match an expected pattern — typically lower priority.
        </span>
      </div>
    </section>
  );
}
