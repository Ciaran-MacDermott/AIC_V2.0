"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import type {
  BrandOverrideConfig,
  JobStatus, MismatchCorrection, MismatchGroup,
  Phase2Config, Phase2ScanResult,
} from "@/lib/types";
import { Header } from "@/components/header";
import { FileSlot } from "@/components/upload";
import { ProgressPanel } from "@/components/progress-panel";
import { LogTail } from "@/components/log-tail";
import { MismatchForm } from "@/components/mismatch-form";
import { StageStepper } from "@/components/stage-stepper";
import { RunsSidebar } from "@/components/runs-sidebar";
import { RunErrorDialog } from "@/components/run-error-dialog";
import { recordRun, updateRunState } from "@/lib/recent";
import {
  Phase2AdvancedConfig,
  type BrandOverrideRow,
  type PrivateLabelRules,
} from "@/components/phase2-advanced";


function defaultPlRules(): PrivateLabelRules {
  return {
    walmart: { enabled: true,  label: "PRIVATE LABEL RESTRICTED" },
    cvs:     { enabled: true,  label: "PRIVATE LABEL EXCLUDE" },
    heb:     { enabled: false, label: "PRIVATE LABEL RESTRICTED" },
  };
}

function defaultBrandOverride(): BrandOverrideConfig {
  return {
    // Always-on: brand override rules are part of every Phase 2 run now.
    // An empty rules list is a no-op for the pipeline so this is safe even
    // when the analyst doesn't add any overrides.
    enable: true,
    raw_manufacturer_col: "RAW_MANUFACTURER",
    brand_col: "BRAND",
    tool_brand_col: "TOOL_BRAND",
    rules: [],
  };
}

const POLL_MS = 700;
const TERMINAL = new Set(["done", "error", "stopped"]);


export default function Phase2PageWrapper() {
  return (
    <Suspense fallback={<main className="mx-auto max-w-5xl px-6 py-8 text-sm text-zinc-500">Loading…</main>}>
      <Phase2Page />
    </Suspense>
  );
}

function Phase2Page() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const parentRunId = searchParams.get("parentRunId") ?? "";
  // ?runId=… resumes a Phase 2 run started in another tab or session.
  const initialRunId = searchParams.get("runId");
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [rawUpcCol, setRawUpcCol] = useState("RAW_BRAND");
  const [skipRmrr, setSkipRmrr] = useState(false);
  const [customCollapse, setCustomCollapse] = useState(false);
  const [scan, setScan] = useState<Phase2ScanResult | null>(null);
  const [scanning, setScanning] = useState(false);

  const [advancedExpanded, setAdvancedExpanded] = useState(false);
  const [plRules, setPlRules] = useState<PrivateLabelRules>(defaultPlRules);
  const [plBaseName, setPlBaseName] = useState("");
  const [brandOverride, setBrandOverride] = useState<BrandOverrideConfig>(defaultBrandOverride);
  // Always start with one empty row so the editor is primed and the
  // dropdowns are visible — analysts run override rules every project.
  const [brandOverrideRows, setBrandOverrideRows] = useState<BrandOverrideRow[]>([
    { manufacturer: "", from_brand: "", to_tool_brand: "" },
  ]);

  const [runId, setRunId] = useState<string | null>(initialRunId);
  const [status, setStatus] = useState<JobStatus | null>(null);
  const [groups, setGroups] = useState<MismatchGroup[] | null>(null);
  const [mismatchBrandValues, setMismatchBrandValues] = useState<string[]>([]);
  const [mismatchToolBrandValues, setMismatchToolBrandValues] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [resolving, setResolving] = useState(false);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [postQcFile, setPostQcFile] = useState<File | null>(null);
  const [postQcSubmitting, setPostQcSubmitting] = useState(false);
  const [postQcDownloadUrl, setPostQcDownloadUrl] = useState<string | null>(null);

  const pollRef = useRef<number | null>(null);

  // Scan zip on pick → autodetect RAW UPC + populate dropdowns.  Mirrors
  // the Streamlit page's _load_cols_from_dir auto-population.
  useEffect(() => {
    if (!zipFile) { setScan(null); return; }
    let cancelled = false;
    setScanning(true);
    api.scanPhase2Zip(zipFile)
      .then((s) => {
        if (cancelled) return;
        setScan(s);
        setRawUpcCol(s.default_upc_col || "RAW_BRAND");
        // Pre-fill the Brand Override RAW MFR column from the autodetect.
        if (s.default_manufacturer_col) {
          setBrandOverride((prev) => ({
            ...prev,
            raw_manufacturer_col: s.default_manufacturer_col,
          }));
        }
      })
      .catch(() => { if (!cancelled) setScan(null); })
      .finally(() => { if (!cancelled) setScanning(false); });
    return () => { cancelled = true; };
  }, [zipFile]);

  // ── Poll status while a run is alive ──────────────────────────────────
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

        if (s.state === "done") {
          setDownloadUrl(`/api/runs/${runId}/artifacts/output.xlsx`);
          // Don't return — keep polling so post_qc transitions are observed.
        }
        if (s.state === "post_qc_done") {
          setPostQcDownloadUrl(`/api/runs/${runId}/artifacts/post_qc.zip`);
          return;
        }
        if (s.state === "mismatch_pending" && groups === null) {
          // Lazy-load the groups + dropdown values when the worker hits the pause.
          const m = await api.mismatch(runId);
          if (!cancelled) {
            setGroups(m.groups);
            setMismatchBrandValues(m.brand_values ?? []);
            setMismatchToolBrandValues(m.tool_brand_values ?? []);
          }
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
  }, [runId, groups]);

  function buildConfig(): Phase2Config {
    // Convert the row-shaped rules editor (Streamlit's UX) into the
    // BrandOverrideRule[] phase3_package.transforms.apply_brand_overrides
    // expects (one rule per row, with manufacturers + brand_overrides keys).
    // Rules are always submitted — empty list is a no-op pipeline-side.
    const rules = brandOverrideRows
      .filter((r) => r.manufacturer && r.from_brand && r.to_tool_brand)
      .map((r) => ({
        manufacturers:   [r.manufacturer],
        brand_overrides: { [r.from_brand]: r.to_tool_brand },
      }));

    return {
      raw_upc_pl_brand_col:  rawUpcCol,
      private_label_config:  plRules,
      brand_override_config: { ...brandOverride, enable: true, rules },
      is_custom_collapse:    customCollapse,
      skip_rmrr:             skipRmrr,
      pl_base_name:          plBaseName,
    };
  }

  async function onRun() {
    setError(null);
    setSubmitting(true);
    try {
      const cfg = buildConfig();
      const { run_id } = parentRunId
        ? await api.startPhase2FromParent(parentRunId, cfg)
        : zipFile
          ? await api.startPhase2(zipFile, cfg)
          : (() => { throw new Error("Pick a zip or arrive via Phase 1"); })();
      setRunId(run_id);
      recordRun({
        run_id, phase: "phase2", created_at: Date.now(),
        label: zipFile?.name ?? (parentRunId ? `from ${parentRunId.slice(0, 6)}` : undefined),
        parent_run_id: parentRunId || undefined,
      });
      router.replace(`/phase2?runId=${encodeURIComponent(run_id)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  async function onStop() {
    if (!runId) return;
    try { await api.stop(runId); } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function onResolve(corrections: MismatchCorrection[]) {
    if (!runId) return;
    setResolving(true);
    setError(null);
    try {
      await api.resolveMismatch(runId, { corrections });
      setGroups(null);   // hide the form; the poller will pick up state changes
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setResolving(false);
    }
  }

  async function onReset() {
    if (runId) {
      try { await api.remove(runId); } catch { /* ignore */ }
    }
    setRunId(null);
    setStatus(null);
    setGroups(null);
    setError(null);
    setZipFile(null);
    setScan(null);
    setDownloadUrl(null);
    setPostQcFile(null);
    setPostQcDownloadUrl(null);
    router.replace("/phase2");
  }

  async function onPostQcUpload() {
    if (!runId || !postQcFile) return;
    setPostQcSubmitting(true);
    setError(null);
    try {
      await api.postQcUpload(runId, postQcFile);
      // The poller picks up state=post_qc_running and then post_qc_done.
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPostQcSubmitting(false);
    }
  }

  const isRunning  = !!status && (status.state === "running" || status.state === "queued");
  const isPaused   = !!status && status.state === "mismatch_pending";
  const isTerminal = !!status && TERMINAL.has(status.state);

  return (
    <>
      <Header
        eyebrow="Phase 2 & 3"
        title="Pipeline & QC"
        subtitle="Upload your project zip, review any BRAND / TOOL_BRAND mismatches, and download the cleaned workbook."
      />
      <main className="mx-auto max-w-5xl px-6 pb-12">

      <RunsSidebar currentRunId={runId} />

      {!runId && (
        <section className="surface-card p-7 mb-6 space-y-6 fade-in-up">
          {parentRunId ? (
            <div className="rounded-xl border border-emerald-200 bg-emerald-50 text-emerald-900 p-4 text-sm">
              <div className="font-medium mb-1">
                Phase 1 outputs detected — re-using run <code className="font-mono">{parentRunId}</code>
              </div>
              <div className="text-xs">
                The QC workbook + project files from your Phase 1 run will feed this run directly;
                no re-upload required. Tweak any advanced rules below if needed, then run.
              </div>
            </div>
          ) : (
            <p className="text-sm text-zinc-600">
              Upload a zip containing <code>File_For_Mapping_QC.xlsx</code>,{" "}
              <code>ModelInfo.txt</code>, <code>Attributes.txt</code> and{" "}
              <code>AttributeValues.txt</code>. The pipeline runs Phase 2 (attribute
              assembly) → Phase 3 (quality checks + transformations) and pauses for
              review if any BRAND vs TOOL_BRAND mismatches surface.
            </p>
          )}
          {!parentRunId && (
            <FileSlot
              label="Project zip (.zip)"
              accept=".zip"
              file={zipFile}
              onPick={setZipFile}
            />
          )}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <label className="rounded-xl border border-zinc-200 bg-white p-3 text-sm">
              <div className="font-medium text-zinc-700 mb-1">
                RAW UPC10 column
                {scanning && <span className="text-xs text-zinc-400 ml-2">scanning…</span>}
              </div>
              {scan && scan.raw_upc_columns.length > 0 ? (
                <select
                  value={rawUpcCol}
                  onChange={(e) => setRawUpcCol(e.target.value)}
                  className="w-full border border-zinc-300 rounded px-2 py-1 text-xs"
                >
                  {scan.raw_upc_columns.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={rawUpcCol}
                  onChange={(e) => setRawUpcCol(e.target.value)}
                  className="w-full border border-zinc-300 rounded px-2 py-1 text-xs"
                  placeholder="Pick a zip to auto-populate"
                />
              )}
            </label>
            <label className="rounded-xl border border-zinc-200 bg-white p-3 text-sm flex items-center gap-2">
              <input
                type="checkbox"
                checked={customCollapse}
                onChange={(e) => setCustomCollapse(e.target.checked)}
              />
              <span className="text-zinc-700">Custom SKU collapse</span>
            </label>
            <label className="rounded-xl border border-zinc-200 bg-white p-3 text-sm flex items-center gap-2">
              <input
                type="checkbox"
                checked={skipRmrr}
                onChange={(e) => setSkipRmrr(e.target.checked)}
              />
              <span className="text-zinc-700">Skip RMRR tagging</span>
            </label>
          </div>

          <Phase2AdvancedConfig
            expanded={advancedExpanded}
            onToggle={() => setAdvancedExpanded(!advancedExpanded)}
            scan={scan}
            privateLabelRules={plRules}
            setPrivateLabelRules={setPlRules}
            plBaseName={plBaseName}
            setPlBaseName={setPlBaseName}
            brandOverride={brandOverride}
            setBrandOverride={setBrandOverride}
            brandOverrideRows={brandOverrideRows}
            setBrandOverrideRows={setBrandOverrideRows}
          />

          <div>
            <button
              type="button"
              disabled={(!zipFile && !parentRunId) || submitting}
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
          {status.state === "error" && (
            <RunErrorDialog status={status} onRetry={onReset} />
          )}
          <StageStepper state={status.state} />
          <ProgressPanel status={status} />

          {isPaused && groups && (
            <div className="mb-6">
              <MismatchForm
                groups={groups}
                brandValues={mismatchBrandValues}
                toolBrandValues={mismatchToolBrandValues}
                onResolve={onResolve}
                isSubmitting={resolving}
              />
            </div>
          )}

          {downloadUrl && (
            <div className="rounded-2xl border border-emerald-200 bg-emerald-50/80 backdrop-blur text-emerald-900 p-5 mb-4 fade-in-up">
              <div className="font-medium mb-2">Cleaned output ready.</div>
              <div className="flex items-center gap-3">
                <a
                  href={api.downloadUrl(downloadUrl)}
                  className="btn-primary inline-flex items-center"
                >
                  Download output.xlsx
                </a>
              </div>
            </div>
          )}

          {/* Post-QC re-upload: edit Cleaned Output in Excel, re-upload here,
              receive a zip of per-category CSVs.  Mirrors the Streamlit
              "Re-upload edited output.xlsx" box on lines 1416-1469. */}
          {downloadUrl && status?.state !== "post_qc_done" && (
            <div className="surface-card p-6 mb-4 space-y-4">
              <div className="font-medium text-zinc-800">Post-QC: edit & re-upload</div>
              <p className="text-xs text-zinc-500">
                Edit the Cleaned Output sheet in Excel, save as a new xlsx, then re-upload
                here. The pipeline will re-collapse SKUs and split by category for export.
              </p>
              <FileSlot
                label="Edited workbook (.xlsx)"
                accept=".xlsx,.xls"
                file={postQcFile}
                onPick={setPostQcFile}
              />
              <button
                type="button"
                disabled={!postQcFile || postQcSubmitting}
                onClick={onPostQcUpload}
                className="btn-success"
              >
                {postQcSubmitting ? "Uploading…" : "Finalise & Export"}
              </button>
            </div>
          )}

          {postQcDownloadUrl && (
            <div className="rounded-2xl border border-emerald-200 bg-emerald-50/80 backdrop-blur text-emerald-900 p-5 mb-4 fade-in-up">
              <div className="font-medium mb-2">
                Post-QC export ready —{" "}
                {status?.post_qc_categories?.length ?? 0} categor
                {(status?.post_qc_categories?.length ?? 0) === 1 ? "y" : "ies"}
              </div>
              <a
                href={api.downloadUrl(postQcDownloadUrl)}
                className="btn-primary inline-flex items-center"
              >
                Download AIC_Phase2_3_exports.zip
              </a>
            </div>
          )}

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
            {isPaused && (
              <button
                type="button"
                onClick={onStop}
                className="btn-danger-outline"
              >
                Cancel run
              </button>
            )}
            {isTerminal && (
              <button
                type="button"
                onClick={onReset}
                className="btn-secondary"
              >
                Start over
              </button>
            )}
            <Link
              href="/"
              className="ml-auto text-sm text-zinc-500 hover:text-zinc-900 transition-colors"
            >
              ← Phase 1
            </Link>
          </div>
        </div>
      )}
      </main>
    </>
  );
}
