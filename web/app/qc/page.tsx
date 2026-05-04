"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import type { QcSheetList, QcSheetPayload, QcSheetSummary } from "@/lib/types";
import { Header } from "@/components/header";
import { QcGrid } from "@/components/qc-grid";

const SAVE_DEBOUNCE_MS = 600;


export default function QcWizardPageWrapper() {
  return (
    <Suspense fallback={<main className="mx-auto max-w-7xl px-6 py-8 text-sm text-zinc-500">Loading…</main>}>
      <QcWizardPage />
    </Suspense>
  );
}

function QcWizardPage() {
  const searchParams = useSearchParams();
  const runId = searchParams.get("runId") ?? "";

  const [sheetList, setSheetList] = useState<QcSheetSummary[] | null>(null);
  const [step, setStep] = useState(0);
  const [payload, setPayload] = useState<QcSheetPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [finalising, setFinalising] = useState(false);

  // Buffer of unsaved edits per sheet, keyed by row_id → attribute_value.
  const pendingEdits = useRef<Map<string, string>>(new Map());
  const saveTimer = useRef<number | null>(null);

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

  // ── Load the current sheet whenever step changes ───────────────────────
  useEffect(() => {
    if (!sheetList || step >= sheetList.length) return;
    const key = sheetList[step].key;
    let cancelled = false;
    pendingEdits.current.clear();
    api.qcSheet(runId, key)
      .then((res) => {
        if (cancelled) return;
        setPayload(res);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    return () => { cancelled = true; };
  }, [runId, sheetList, step]);

  const flushEdits = useCallback(async () => {
    if (saveTimer.current) {
      window.clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    if (!payload || pendingEdits.current.size === 0) return;
    const editedRows = [...pendingEdits.current.entries()].map(
      ([row_id, attribute_value]) => ({ row_id, attribute_value }),
    );
    pendingEdits.current.clear();
    try {
      await api.qcSave(runId, payload.key, { edited_rows: editedRows });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [payload, runId]);

  function onEdit(rowId: string, value: string) {
    pendingEdits.current.set(rowId, value);
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(flushEdits, SAVE_DEBOUNCE_MS);
  }

  async function next() {
    await flushEdits();
    if (!sheetList) return;
    if (step + 1 < sheetList.length) {
      setStep(step + 1);
    } else {
      await finalize();
    }
  }

  async function skip() {
    await flushEdits();
    await finalize();
  }

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
  const stepProgress = useMemo(() => total === 0 ? 0 : step / total, [step, total]);

  return (
    <>
      <Header
        eyebrow="Phase 1 — QC review"
        title="QC Lookup Review"
        subtitle="Review and correct each lookup sheet before downloading the final workbook."
      />
      <main className="mx-auto max-w-7xl px-6 pb-12">
      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50/80 backdrop-blur text-red-800 p-4 mb-4 text-sm">
          {error}
        </div>
      )}

      {downloadUrl ? (
        <section className="rounded-2xl border border-emerald-200 bg-emerald-50/80 backdrop-blur text-emerald-900 p-5 fade-in-up">
          <div className="font-medium mb-2">QC workbook ready.</div>
          <div className="flex flex-wrap items-center gap-3">
            <a
              href={api.downloadUrl(downloadUrl)}
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
            <a
              href={api.downloadUrl(`/api/runs/${runId}/artifacts/bundle.zip`)}
              className="text-sm text-brand-700 hover:text-brand-900 underline"
            >
              Download bundle
            </a>
            <a
              href={api.downloadUrl(`/api/runs/${runId}/artifacts/log.txt`)}
              className="text-sm text-brand-700 hover:text-brand-900 underline"
            >
              Download log
            </a>
            <Link
              href="/"
              className="text-sm text-brand-700 hover:text-brand-900 underline"
            >
              Start a new run
            </Link>
          </div>
        </section>
      ) : !sheetList ? (
        <div className="text-sm text-zinc-500">Loading sheets…</div>
      ) : sheetList.length === 0 ? (
        <div className="text-sm text-zinc-500">
          No QC sheets — the pipeline produced an empty ensemble.
        </div>
      ) : (
        <div className="fade-in-up">
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
