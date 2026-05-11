"use client";

// Phase 1 page — file upload (xlsx+csv or zip) → POST /api/phase1/runs →
// poll /api/runs/{id} every POLL_MS until qc_ready (auto-routes to /qc)
// or a terminal state. Deep-linkable via ?runId=… so analysts can share
// links and resume after a tab close.

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import type { JobStatus } from "@/lib/types";
import { Header } from "@/components/header";
import { FileSlot } from "@/components/upload";
import { ProgressPanel } from "@/components/progress-panel";
import { FullLogTail } from "@/components/log-tail";
import { RunErrorDialog } from "@/components/run-error-dialog";

const POLL_MS = 700;
// Terminal-for-this-page states. qc_ready and mismatch_pending route to
// /qc and /phase2 respectively, not "the run failed" — they just stop polling here.
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
  const [logsOpen, setLogsOpen] = useState(false);

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
        if (s.state === "qc_ready") {
          router.push(`/qc?runId=${encodeURIComponent(runId)}`);
          return;
        }
        if (TERMINAL.has(s.state)) return;
        pollRef.current = window.setTimeout(tick, POLL_MS);
      } catch (e) {
        if (cancelled) return;
        // 404 on the URL's runId means the run was finished and cleaned
        // up (or evicted by idle TTL).  Don't render an error — silently
        // reset to the upload form so re-opening a stale link from a
        // bookmark / shared message lands clean.
        const msg = e instanceof Error ? e.message : String(e);
        if (msg.startsWith("404")) {
          setRunId(null);
          setStatus(null);
          router.replace("/");
          return;
        }
        setError(msg);
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
        subtitle={
          <>
            Upload an Excel with FINAL and META sheets and a new-product CSV — or a zip with those plus all tool files (recommended) to run all phases.
            <span className="block mt-2 text-[14px] text-zinc-500">
              Suggestions powered by lookups + ML; review and finalise one QC sheet per attribute.
            </span>
          </>
        }
      />
      <main className="mx-auto max-w-5xl px-6 pb-12">

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
          {/* Logs collapsed by default — keeps the run UI focused on stage +
              progress, while leaving the full output one click away.
              Mirrors the QC page's pipelineStrip and Phase 2 advanced config
              disclosure patterns. */}
          {status.log_cursor > 0 && runId && (
            <details
              open={logsOpen}
              onToggle={(e) => setLogsOpen((e.target as HTMLDetailsElement).open)}
              className="group surface-card-quiet mt-3 px-4 py-2 text-xs text-zinc-600"
            >
              <summary className="flex cursor-pointer list-none items-center justify-between gap-3 select-none">
                <span className="flex items-center gap-2">
                  <span className={`inline-block h-1.5 w-1.5 rounded-full ${
                    isError ? "bg-err"
                    : isStopped ? "bg-zinc-400"
                    : isRunning ? "bg-brand-500"
                    : "bg-emerald-500"
                  }`} />
                  <span>
                    Pipeline QC output
                    <span className="text-zinc-400"> · </span>
                    <span className="tabular-nums">{status.log_cursor}</span> log lines
                  </span>
                </span>
                <span className="text-zinc-400 group-open:hidden">Show ▾</span>
                <span className="text-zinc-400 hidden group-open:inline">Hide ▴</span>
              </summary>
              {/* Mount-on-open: FullLogTail fetches the whole buffer via
                  /api/runs/{id}/logs — analyst sees every line, not just
                  the live 60-line tail in the status response. */}
              <div className="mt-3">
                {logsOpen && (
                  <FullLogTail runId={runId} active={isRunning} />
                )}
              </div>
            </details>
          )}
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
