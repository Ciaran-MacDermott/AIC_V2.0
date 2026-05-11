"use client";

// QC wizard — step through each lookup sheet from /api/runs/{id}/qc/sheets.
// Edits buffer in pendingEdits and auto-save (PUT) on a SAVE_DEBOUNCE_MS
// debounce. Finalize POSTs and surfaces the workbook download URL.

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import type { JobStatus, QcSheetList, QcSheetPayload, QcSheetSummary } from "@/lib/types";
import { Header } from "@/components/header";
import { QcGrid } from "@/components/qc-grid";
import { LogTail } from "@/components/log-tail";

const SAVE_DEBOUNCE_MS = 600;
// Workflow-end teardown delay. Gives the browser headroom for a cold first
// byte before the run is deleted server-side.
const RESET_DELAY_MS = 6000;


export default function QcWizardPageWrapper() {
  return (
    <Suspense fallback={<main className="mx-auto max-w-7xl px-6 py-8 text-sm text-zinc-500">Loading…</main>}>
      <QcWizardPage />
    </Suspense>
  );
}

function QcWizardPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const runId = searchParams.get("runId") ?? "";

  // Plain Download click = "I'm done": wait so the file actually starts
  // streaming, then delete the run server-side and reset. The Continue-to-
  // Phase-2 button navigates away before this timer fires.
  function finishAndReset(): void {
    window.setTimeout(() => {
      if (runId) api.remove(runId).catch(() => undefined);
      router.replace("/");
    }, RESET_DELAY_MS);
  }

  const [sheetList, setSheetList] = useState<QcSheetSummary[] | null>(null);
  const [step, setStep] = useState(0);
  const [payload, setPayload] = useState<QcSheetPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [finalising, setFinalising] = useState(false);
  const [status, setStatus] = useState<JobStatus | null>(null);

  // Unsaved edits for the current sheet (cleared per sheet, line ~97).
  // Keyed by row_id → attribute_value.
  const pendingEdits = useRef<Map<string, string>>(new Map());
  const saveTimer = useRef<number | null>(null);

  // Save state — surfaced near the grid so analysts editing at the bottom
  // of a long sheet see save activity and failures without scrolling up.
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  // Mirror pendingEdits.current.size into render state so the indicator
  // updates as analysts type.  Refs alone don't trigger re-renders.
  const [pendingCount, setPendingCount] = useState(0);

  // ── Initial load: list of sheets ───────────────────────────────────────
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    api.qcSheets(runId)
      .then((res: QcSheetList) => {
        if (cancelled) return;
        setSheetList(res.sheets);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    return () => { cancelled = true; };
  }, [runId]);

  // Fetch the Phase 1 job snapshot once so we can surface a subtle
  // "pipeline complete" indicator + the log tail on demand.  Mirrors the
  // Streamlit page's persistent log box but kept minimal — the analyst
  // is now focused on QC, not on watching output scroll past.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    api.status(runId)
      .then((s) => { if (!cancelled) setStatus(s); })
      .catch(() => { /* non-fatal — log strip just won't render */ });
    return () => { cancelled = true; };
  }, [runId]);

  // ── Load the current sheet whenever step changes ───────────────────────
  useEffect(() => {
    if (!sheetList || step >= sheetList.length) return;
    const key = sheetList[step].key;
    let cancelled = false;
    pendingEdits.current.clear();
    setPendingCount(0);
    setSaveError(null);
    api.qcSheet(runId, key)
      .then((res) => {
        if (cancelled) return;
        setPayload(res);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    return () => { cancelled = true; };
  }, [runId, sheetList, step]);

  // Returns true on success, false on failure — callers that gate sheet
  // navigation use the return value to keep the analyst on the current
  // sheet so unsaved edits aren't silently lost.
  const flushEdits = useCallback(async (): Promise<boolean> => {
    if (saveTimer.current) {
      window.clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    if (!payload || pendingEdits.current.size === 0) return true;
    const editedRows = [...pendingEdits.current.entries()].map(
      ([row_id, attribute_value]) => ({ row_id, attribute_value }),
    );
    setSaving(true);
    setSaveError(null);
    try {
      await api.qcSave(runId, payload.key, { edited_rows: editedRows });
      pendingEdits.current.clear();
      setPendingCount(0);
      return true;
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setSaveError(msg);
      // Keep edits in the buffer so a retry-on-next can recover.
      return false;
    } finally {
      setSaving(false);
    }
  }, [payload, runId]);

  function onEdit(rowId: string, value: string) {
    pendingEdits.current.set(rowId, value);
    setPendingCount(pendingEdits.current.size);
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(flushEdits, SAVE_DEBOUNCE_MS);
  }

  async function next() {
    const ok = await flushEdits();
    if (!ok) return;  // stay on this sheet so the analyst can retry / fix
    if (!sheetList) return;
    if (step + 1 < sheetList.length) {
      setStep(step + 1);
    } else {
      await finalize();
    }
  }

  async function skip() {
    const ok = await flushEdits();
    if (!ok) return;
    await finalize();
  }

  // Browser-level guardrail: warn if the analyst tries to refresh, close
  // the tab, or navigate away while edits are unsaved (debounce window not
  // yet flushed, or a save failed and the buffer still holds edits).
  useEffect(() => {
    function handler(e: BeforeUnloadEvent): string | undefined {
      if (pendingCount > 0 || saving) {
        e.preventDefault();
        e.returnValue = "";  // required by spec — modern browsers show their own message
        return "";
      }
      return undefined;
    }
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [pendingCount, saving]);

  async function finalize() {
    setFinalising(true);
    setError(null);
    try {
      const res = await api.qcFinalize(runId);
      setDownloadUrl(res.download_url);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setFinalising(false);
    }
  }

  const total = sheetList?.length ?? 0;
  const currentSheet = sheetList?.[step];
  const stepProgress = total === 0 ? 0 : step / total;

  const pipelineStrip = status && status.log_tail.length > 0 ? (
    <details className="group surface-card-quiet mb-3 px-4 py-2 text-xs text-zinc-600">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 select-none">
        <span className="flex items-center gap-2">
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500" />
          <span>
            Pipeline complete
            <span className="text-zinc-400"> · </span>
            <span className="tabular-nums">{Math.round(status.elapsed_s)}s</span>
            <span className="text-zinc-400"> · </span>
            <span className="tabular-nums">{status.log_cursor}</span> log lines
          </span>
        </span>
        <span className="text-zinc-400 group-open:hidden">Show output ▾</span>
        <span className="text-zinc-400 hidden group-open:inline">Hide output ▴</span>
      </summary>
      <div className="mt-3">
        <LogTail lines={status.log_tail} />
      </div>
    </details>
  ) : null;

  return (
    <>
      <Header
        eyebrow="Phase 1 — QC review"
        title="QC Lookup Review"
        subtitle="Review and correct each lookup sheet before downloading the final workbook."
      />
      <main className="mx-auto max-w-7xl px-6 pb-12">
      {finalising && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-900/40 backdrop-blur-sm fade-in-up"
          role="dialog"
          aria-live="polite"
          aria-busy="true"
        >
          <div className="surface-card flex max-w-sm items-center gap-3 px-6 py-5">
            <div className="h-5 w-5 animate-spin rounded-full border-2 border-brand-200 border-t-brand-700" />
            <div>
              <div className="font-medium text-zinc-900">Saving and finalizing…</div>
              <div className="text-xs text-zinc-500">Writing File_For_Mapping_QC.xlsx</div>
            </div>
          </div>
        </div>
      )}
      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50/80 backdrop-blur text-red-800 p-4 mb-4 text-sm">
          {error}
        </div>
      )}

      {downloadUrl ? (
        <>
        {pipelineStrip}
        <section className="rounded-2xl border border-emerald-200 bg-emerald-50/80 backdrop-blur text-emerald-900 p-5 fade-in-up">
          <div className="font-medium mb-2">QC workbook ready.</div>
          <p className="text-xs text-emerald-900/70 mb-3">
            If you&apos;re continuing to Phase 2 / 3, click that button first — a plain Download
            click means you&apos;re done and will reset this page.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            {/* Plain download = "I'm done" — auto-reset after the browser
                starts the file.  Phase 1 → Phase 2 users should click the
                Continue button first; that navigates away before the timer
                fires, so the run isn't deleted out from under them. */}
            <a
              href={api.downloadUrl(downloadUrl)}
              onClick={finishAndReset}
              className="btn-primary inline-flex items-center"
            >
              Download File_For_Mapping_QC.xlsx
            </a>
            {/* Hand the QC workbook + any extra files in this run's tmpdir
                straight to Phase 2/3 via the parent_run_id route — no
                re-upload needed.  Mirrors the Streamlit handoff where the
                Phase 1 zip extract is reused for Phase 2. */}
            <Link
              href={`/phase2?parentRunId=${encodeURIComponent(runId)}`}
              className="btn-base bg-zinc-900 text-white hover:bg-zinc-700 inline-flex items-center"
            >
              Continue to Phase 2 / 3 →
            </Link>
            {/* Bundle is the audit-trail download — no need for a
                standalone log link on Phase 1 since the bundle already
                contains it.  The label spells out the contents so
                analysts know what's in the zip without unpacking it. */}
            <a
              href={api.downloadUrl(`/api/runs/${runId}/artifacts/bundle.zip`)}
              className="text-sm text-brand-700 hover:text-brand-900 underline"
              title="QC workbook + run log + analyst edits + metadata"
            >
              Download archive (.zip — workbook, log, edits)
            </a>
          </div>
        </section>
        </>
      ) : !sheetList ? (
        <div className="text-sm text-zinc-500">Loading sheets…</div>
      ) : sheetList.length === 0 ? (
        <div className="text-sm text-zinc-500">
          No QC sheets — the pipeline produced an empty ensemble.
        </div>
      ) : (
        <div className="fade-in-up">
          {pipelineStrip}
          <div className="surface-card p-5 mb-4">
            <div className="mb-2 flex items-center justify-between text-sm text-zinc-600">
              <span>
                Sheet <strong className="text-zinc-900">{step + 1}</strong> of {total}
                {currentSheet && (
                  <>: <code className="font-mono text-brand-700">{currentSheet.label}</code></>
                )}
              </span>
              <span className="tabular-nums text-zinc-500">{Math.round(stepProgress * 100)}%</span>
            </div>
            <div className="h-1.5 rounded-full bg-brand-100 overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-brand-500 to-brand-700 transition-all duration-500"
                style={{ width: `${Math.round(stepProgress * 100)}%` }}
              />
            </div>
          </div>

          {/* Dirty-state strip — sticky-ish placement above the grid so
              analysts editing the bottom of a long sheet still notice
              save activity, transient failures, or unsaved buffer. */}
          {(saving || pendingCount > 0 || saveError) && (
            <div
              className={`mb-2 flex items-center gap-2 rounded-lg border px-3 py-1.5 text-xs ${
                saveError
                  ? "border-red-200 bg-red-50 text-red-800"
                  : saving
                    ? "border-brand-200 bg-brand-50 text-brand-800"
                    : "border-amber-200 bg-amber-50 text-amber-900"
              }`}
              role="status"
              aria-live="polite"
            >
              {saving ? (
                <>
                  <span className="h-3 w-3 animate-spin rounded-full border-2 border-brand-300 border-t-brand-700" />
                  <span>Saving {pendingCount} edit{pendingCount === 1 ? "" : "s"}…</span>
                </>
              ) : saveError ? (
                <>
                  <span aria-hidden>⚠</span>
                  <span>Save failed: {saveError}.</span>
                  <button
                    type="button"
                    onClick={() => flushEdits()}
                    className="ml-auto underline hover:no-underline"
                  >
                    Retry
                  </button>
                </>
              ) : (
                <>
                  <span className="inline-block h-2 w-2 rounded-full bg-amber-500" />
                  <span>{pendingCount} unsaved edit{pendingCount === 1 ? "" : "s"} (saving in ~{SAVE_DEBOUNCE_MS / 1000}s).</span>
                </>
              )}
            </div>
          )}

          {payload ? (
            <QcGrid payload={payload} onEdit={onEdit} />
          ) : (
            <div className="text-sm text-zinc-500">Loading sheet…</div>
          )}

          <div className="mt-4 flex items-center gap-2">
            <button
              type="button"
              onClick={next}
              disabled={finalising}
              className="btn-primary"
            >
              {step + 1 < total ? "Save & Next" : "Save & Finalize"}
            </button>
            {step + 1 < total && (
              <button
                type="button"
                onClick={skip}
                disabled={finalising}
                className="btn-secondary"
              >
                Skip remaining & finalize
              </button>
            )}
            <Link
              href="/"
              className="ml-auto text-sm text-zinc-500 hover:text-zinc-900 transition-colors"
            >
              Cancel
            </Link>
          </div>
        </div>
      )}
      </main>
    </>
  );
}
