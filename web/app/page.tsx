"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import type { JobStatus } from "@/lib/types";
import { Header } from "@/components/header";
import { FileSlot } from "@/components/upload";
import { ProgressPanel } from "@/components/progress-panel";
import { LogTail } from "@/components/log-tail";
import { RunsSidebar } from "@/components/runs-sidebar";
import { RunErrorDialog } from "@/components/run-error-dialog";
import { recordRun, updateRunState } from "@/lib/recent";

const POLL_MS = 700;
// Terminal states the polling loop can stop on.
const TERMINAL = new Set(["done", "error", "stopped", "qc_ready", "mismatch_pending"]);

export default function Phase1PageWrapper() {
  // useSearchParams() forces a Suspense boundary under Next 15 static export.
  return (
    <Suspense fallback={<main className="mx-auto max-w-5xl px-6 py-8" />}>
      <Phase1Page />
    </Suspense>
  );
}

function Phase1Page() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // Deep-linking: ?runId=… resumes an existing run instead of showing the
  // upload form.  Lets analysts share links and recover after a tab close.
  const initialRunId = searchParams.get("runId");

  const [uploadMode, setUploadMode] = useState<"zip" | "files">("files");
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [xlsx, setXlsx] = useState<File | null>(null);
  const [csv,  setCsv]  = useState<File | null>(null);
  const [runId, setRunId] = useState<string | null>(initialRunId);
  const [status, setStatus] = useState<JobStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const pollRef = useRef<number | null>(null);

  // Poll while there's an active run that hasn't reached qc_ready/done/error.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;

    async function tick() {
      if (cancelled || !runId) return;
      try {
        const s = await api.status(runId);
        if (cancelled) return;
        setStatus(s);
        updateRunState(runId, s.state);
        if (s.state === "qc_ready") {
          router.push(`/qc?runId=${encodeURIComponent(runId)}`);
          return;
        }
        if (TERMINAL.has(s.state)) return;
        pollRef.current = window.setTimeout(tick, POLL_MS);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      }
    }

    tick();
    return () => {
      cancelled = true;
      if (pollRef.current) window.clearTimeout(pollRef.current);
    };
  }, [runId, router]);

  async function onRun() {
    setError(null);
    setSubmitting(true);
    try {
      const { run_id } =
        uploadMode === "zip" && zipFile
          ? await api.startPhase1FromZip(zipFile)
          : xlsx && csv
            ? await api.startPhase1(xlsx, csv)
            : (() => { throw new Error("Pick a zip OR an xlsx + csv before running"); })();
      setRunId(run_id);
      // Persist the new run so the sidebar (and a future tab) can find it.
      const label = uploadMode === "zip"
        ? zipFile?.name
        : xlsx ? `${xlsx.name}` : undefined;
      recordRun({
        run_id, phase: "phase1", created_at: Date.now(), label,
      });
      // Reflect the run in the URL so reloads / shares work.
      router.replace(`/?runId=${encodeURIComponent(run_id)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  async function onStop() {
    if (!runId) return;
    try {
      await api.stop(runId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function onReset() {
    if (runId) {
      try { await api.remove(runId); } catch { /* ignore */ }
    }
    setRunId(null);
    setStatus(null);
    setError(null);
    setXlsx(null);
    setCsv(null);
    setZipFile(null);
    router.replace("/");
  }

  const canRun =
    uploadMode === "zip" ? !!zipFile : !!xlsx && !!csv;

  const isRunning = !!status && (status.state === "running" || status.state === "queued");
  const isError   = !!status && status.state === "error";
  const isStopped = !!status && status.state === "stopped";

  return (
    <>
      <Header
        eyebrow="Phase 1"
        title="Attribute Mapping"
        subtitle="Upload a labelled Excel (META + FINAL) and a new-product CSV. The pipeline runs lookup → BM25 → XGBoost ensemble, then surfaces each attribute's lookup sheet for QC review."
      />
      <main className="mx-auto max-w-5xl px-6 pb-12">
      <RunsSidebar currentRunId={runId} />

      {!runId && (
        <section className="surface-card p-7 mb-6 space-y-6 fade-in-up">
          <div className="inline-flex items-center gap-1 rounded-lg border border-zinc-200 bg-white p-1 text-sm">
            <button
              type="button"
              onClick={() => setUploadMode("files")}
              className={
                uploadMode === "files"
                  ? "rounded-md px-3 py-1.5 bg-brand-700 text-white font-medium shadow-sm"
                  : "rounded-md px-3 py-1.5 text-zinc-600 hover:text-zinc-900"
              }
            >
              Individual files
            </button>
            <button
              type="button"
              onClick={() => setUploadMode("zip")}
              className={
                uploadMode === "zip"
                  ? "rounded-md px-3 py-1.5 bg-brand-700 text-white font-medium shadow-sm"
                  : "rounded-md px-3 py-1.5 text-zinc-600 hover:text-zinc-900"
              }
            >
              Project ZIP
            </button>
          </div>

          {uploadMode === "files" ? (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <FileSlot
                label="Excel file (.xlsx)"
                accept=".xlsx,.xls"
                file={xlsx}
                onPick={setXlsx}
              />
              <FileSlot
                label="CSV flat file (.csv)"
                accept=".csv"
                file={csv}
                onPick={setCsv}
              />
            </div>
          ) : (
            <div>
              <FileSlot
                label="Project zip — Excel (META + FINAL) + CSV + Phase 2/3 txt files"
                accept=".zip"
                file={zipFile}
                onPick={setZipFile}
              />
              <p className="mt-2 text-xs text-zinc-500">
                The zip contents seed both Phase 1 and Phase 2/3 — after Phase 1 finishes,
                the same project folder feeds the Phase 2 page without re-uploading.
              </p>
            </div>
          )}

          <div>
            <button
              type="button"
              disabled={!canRun || submitting}
              onClick={onRun}
              className="btn-success"
            >
              {submitting ? "Starting…" : "Run pipeline"}
            </button>
          </div>
        </section>
      )}

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50/80 backdrop-blur text-red-800 p-4 mb-4 text-sm">
          {error}
        </div>
      )}

      {status && (
        <div className="fade-in-up">
          {isError && (
            <RunErrorDialog status={status} onRetry={onReset} />
          )}
          <ProgressPanel status={status} />
          <LogTail lines={status.log_tail} />
          <div className="mt-4 flex items-center gap-2">
            {isRunning && (
              <button
                type="button"
                onClick={onStop}
                className="btn-danger-outline"
              >
                Stop
              </button>
            )}
            {/* "Start over" lives inside the error dialog now; keep it
                here only for the stopped-by-user path. */}
            {isStopped && (
              <button
                type="button"
                onClick={onReset}
                className="btn-secondary"
              >
                Start over
              </button>
            )}
            <Link
              href="/phase2"
              className="ml-auto text-sm text-zinc-500 hover:text-zinc-900 transition-colors"
            >
              Phase 2 & 3 →
            </Link>
          </div>
        </div>
      )}

      {!status && !runId && (
        <div className="mt-6 text-xs text-zinc-500">
          Already finished Phase 1?{" "}
          <Link href="/phase2" className="underline hover:text-zinc-900">
            Continue to Phase 2 & 3
          </Link>
          .
        </div>
      )}
      </main>
    </>
  );
}
