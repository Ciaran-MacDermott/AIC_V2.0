"use client";

// Phase 2/3 page — zip upload (or handoff from Phase 1 via ?parentRunId=…)
// → scan → run → optional BRAND/TOOL_BRAND mismatch review → cleaned-output
// download → post-QC re-upload as a standalone run. Two independent poll
// loops: runId (Phase 2) and postQcRunId (post-QC).

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
import { FullLogTail } from "@/components/log-tail";
import { MismatchForm } from "@/components/mismatch-form";
import { StageStepper } from "@/components/stage-stepper";
import { RunErrorDialog } from "@/components/run-error-dialog";
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

// Brand-override rules are submitted every run; empty rules is a pipeline
// no-op. brand_col/tool_brand_col aren't in the schema — pipeline resolves
// them from Attributes.txt at run time.
function defaultBrandOverride(): BrandOverrideConfig {
  return {
    enable: true,
    raw_manufacturer_col: "RAW_MANUFACTURER",
    raw_parent_col: "RAW_PARENT",
    rules: [],
  };
}

const POLL_MS = 700;
// Terminal-for-this-page states. mismatch_pending isn't here because it
// blocks the worker on resume_event, which the wizard resolves below.
const TERMINAL = new Set(["done", "error", "stopped"]);
// Workflow-end teardown delay — gives the browser headroom for cold first
// byte on the final download before the run is removed server-side.
const RESET_DELAY_MS = 6000;


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
  // Post-QC re-upload runs as a standalone job so it isn't blocked by the
  // original Phase 2 run being evicted (60min idle TTL or BFF restart).
  // Tracked separately from `runId` so the user can still re-download the
  // original output.xlsx while post-QC progresses.
  const [postQcRunId, setPostQcRunId] = useState<string | null>(null);
  const [postQcStatus, setPostQcStatus] = useState<JobStatus | null>(null);

  const pollRef = useRef<number | null>(null);
  const postQcPollRef = useRef<number | null>(null);
  const [logsOpen, setLogsOpen] = useState(false);

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
        // Pre-fill manufacturer + parent column pickers from the autodetect
        // so the analyst doesn't have to set them manually on every run.
        setBrandOverride((prev) => ({
          ...prev,
          ...(s.default_manufacturer_col && { raw_manufacturer_col: s.default_manufacturer_col }),
          ...(s.default_parent_col && { raw_parent_col: s.default_parent_col }),
        }));
      })
      .catch(() => { if (!cancelled) setScan(null); })
      .finally(() => { if (!cancelled) setScanning(false); });
    return () => { cancelled = true; };
  }, [zipFile]);

  // Handoff flow (?parentRunId=…): scan the parent's QC workbook so the
  // advanced-config dropdowns (BRAND/TOOL_BRAND/manufacturer rule editor)
  // populate without the user having to re-upload anything.  Mirrors the
  // zipFile scan above; both paths land on the same `scan` state so the
  // child components don't care which one ran.
  useEffect(() => {
    if (!parentRunId) return;
    let cancelled = false;
    setScanning(true);
    api.scanPhase2FromParent(parentRunId)
      .then((s) => {
        if (cancelled) return;
        setScan(s);
        setRawUpcCol(s.default_upc_col || "RAW_BRAND");
        setBrandOverride((prev) => ({
          ...prev,
          ...(s.default_manufacturer_col && { raw_manufacturer_col: s.default_manufacturer_col }),
          ...(s.default_parent_col && { raw_parent_col: s.default_parent_col }),
        }));
      })
      .catch(() => { if (!cancelled) setScan(null); })
      .finally(() => { if (!cancelled) setScanning(false); });
    return () => { cancelled = true; };
  }, [parentRunId]);

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

        if (s.state === "done") {
          setDownloadUrl(`/api/runs/${runId}/artifacts/output.xlsx`);
          // Phase 2 done is terminal here — post-QC runs as a standalone
          // job tracked via postQcRunId, not on this record.
          return;
        }
        if (s.state === "mismatch_pending" && groups === null) {
          // Lazy-load groups + dropdown values when the worker hits the pause.
          // `groups` is in the effect dep array (see below) so this branch
          // re-fires once after groups are stored; the cancelled flag guards.
          const mismatchPayload = await api.mismatch(runId);
          if (!cancelled) {
            setGroups(mismatchPayload.groups);
            setMismatchBrandValues(mismatchPayload.brand_values ?? []);
            setMismatchToolBrandValues(mismatchPayload.tool_brand_values ?? []);
          }
        }
        if (TERMINAL.has(s.state)) return;
        pollRef.current = window.setTimeout(tick, POLL_MS);
      } catch (e) {
        if (cancelled) return;
        // Stale link / cleaned-up run — silently reset rather than
        // surfacing "Run not found" to a user re-opening a bookmark.
        const msg = e instanceof Error ? e.message : String(e);
        if (msg.startsWith("404")) {
          setRunId(null);
          setStatus(null);
          setDownloadUrl(null);
          setGroups(null);
          router.replace("/phase2");
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
  }, [runId, groups, router]);

  // Independent poll for the standalone post-QC run.  Lives on its own
  // run_id so the original Phase 2 record's state isn't disturbed.
  useEffect(() => {
    if (!postQcRunId) return;
    let cancelled = false;

    async function tick() {
      if (cancelled || !postQcRunId) return;
      try {
        const s = await api.status(postQcRunId);
        if (cancelled) return;
        setPostQcStatus(s);
        if (s.state === "post_qc_done") {
          setPostQcDownloadUrl(`/api/runs/${postQcRunId}/artifacts/post_qc.zip`);
          return;
        }
        if (TERMINAL.has(s.state)) return;
        postQcPollRef.current = window.setTimeout(tick, POLL_MS);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      }
    }

    tick();
    return () => {
      cancelled = true;
      if (postQcPollRef.current) window.clearTimeout(postQcPollRef.current);
    };
  }, [postQcRunId]);

  // Browser-level guardrail: if the wizard is open (groups loaded) the
  // analyst has either started reviewing or has accumulated edits across
  // groups.  Refresh / close-tab / browser-back drops all of that state
  // because corrections live only in MismatchForm's local React state
  // until the analyst submits the last group.  Warn before unload so the
  // analyst can confirm rather than lose silently.
  //
  // Only attached while groups !== null — when the wizard isn't open the
  // page is safe to navigate away from.
  useEffect(() => {
    if (groups === null) return undefined;
    function handler(e: BeforeUnloadEvent): string | undefined {
      e.preventDefault();
      e.returnValue = "";  // required by spec for the browser-native prompt
      return "";
    }
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [groups]);

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
    // Best-effort delete every run we know about for this workflow so
    // the server's tmpdirs (and the in-memory registry slots) come back
    // immediately — analysts don't have to wait for the 60min idle-TTL
    // sweep.  Errors are swallowed: a 404 just means the run was
    // already evicted, which is the desired terminal state anyway.
    const ids = [runId, postQcRunId, parentRunId].filter(Boolean) as string[];
    await Promise.all(
      ids.map((id) => api.remove(id).catch(() => undefined)),
    );
    setRunId(null);
    setStatus(null);
    setGroups(null);
    setError(null);
    setZipFile(null);
    setScan(null);
    setDownloadUrl(null);
    setPostQcFile(null);
    setPostQcDownloadUrl(null);
    setPostQcRunId(null);
    setPostQcStatus(null);
    router.replace("/phase2");
  }

  // Plain final-download click = "I'm done": wait so the file actually
  // starts streaming, then run onReset to free server-side tmpdirs.
  function finishAndReset(): void {
    window.setTimeout(() => { void onReset(); }, RESET_DELAY_MS);
  }

  async function onPostQcUpload() {
    if (!postQcFile) return;
    setPostQcSubmitting(true);
    setError(null);
    try {
      // Standalone post-QC creates a fresh run for the edited xlsx; not
      // affected by the original Phase 2 run being evicted (60min idle
      // TTL) or a BFF restart between Phase 2 finishing and the upload.
      const { run_id } = await api.postQcStandalone(postQcFile, customCollapse);
      setPostQcRunId(run_id);
      // Polling picks up here via the postQcRunId effect.
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPostQcSubmitting(false);
    }
  }

  const isRunning  = !!status && (status.state === "running" || status.state === "queued");
  const isPaused   = !!status && status.state === "mismatch_pending";
  const isTerminal = !!status && TERMINAL.has(status.state);
  const isError    = !!status && status.state === "error";
  const isStopped  = !!status && status.state === "stopped";

  return (
    <>
      <Header
        eyebrow="Phase 2 & 3"
        title="Pipeline & QC"
        subtitle={"Run Phase 2 (attribute assembly) → Phase 3 (quality checks) on a zipped project.\nResolve any BRAND vs TOOL_BRAND mismatches when prompted, then QC the cleaned workbook and re-upload to export per-category CSVs."}
      />
      <main className="mx-auto max-w-5xl px-6 pb-12">

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
              Zip contents: <code>File_For_Mapping_QC.xlsx</code> +{" "}
              <code>ModelInfo.txt</code>, <code>Attributes.txt</code>,{" "}
              <code>AttributeValues.txt</code>.
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

          {/* Scan summary — shows what the backend resolved from each
              Attributes.txt as soon as the scan completes.  Surfaces the
              brand pair(s) at the page level (not just inside the
              collapsed advanced panel) so analysts can confirm the
              project shape before kicking off a run. */}
          {scan && scan.detected_brand_pairs && scan.detected_brand_pairs.length > 0 && (
            <div className="rounded-xl border border-emerald-200 bg-emerald-50/70 px-4 py-3 text-xs text-emerald-900 fade-in-up">
              <div className="font-medium mb-1">
                Detected {scan.detected_brand_pairs.length} brand pair{scan.detected_brand_pairs.length === 1 ? "" : "s"} from Attributes.txt
              </div>
              <div className="font-mono">
                {scan.detected_brand_pairs
                  .map((p) => `${p.brand_col} / ${p.tool_brand_col}`)
                  .join("  •  ")}
              </div>
            </div>
          )}
          {scan && (!scan.detected_brand_pairs || scan.detected_brand_pairs.length === 0) && (
            <div className="rounded-xl border border-amber-200 bg-amber-50/70 px-4 py-3 text-xs text-amber-900 fade-in-up">
              No <code>Brand_Attribute=Y</code> row found in any Attributes.txt — the run will fall back
              to literal <code>BRAND</code> / <code>TOOL_BRAND</code> columns.  Confirm this is expected
              before running.
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <label className="rounded-xl border border-zinc-200 bg-white p-3 text-sm">
              <div className="font-medium text-zinc-700 mb-1">
                Private-label detection col
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
              <div className="flex flex-wrap items-center gap-3">
                <a
                  href={api.downloadUrl(downloadUrl)}
                  className="btn-primary inline-flex items-center"
                >
                  Download output.xlsx
                </a>
                {/* Run log alongside the xlsx — analysts use the log to
                    inform their QC pass before re-uploading.  Same UTF-8
                    log content the progress panel exposes; surfaced here
                    too so it's a single click from the cleaned output. */}
                {runId && (
                  <a
                    href={api.downloadUrl(`/api/runs/${runId}/artifacts/log.txt`)}
                    className="btn-secondary inline-flex items-center"
                  >
                    Download run log (.txt)
                  </a>
                )}
              </div>
              <p className="mt-2 text-xs text-emerald-900/70">
                Grab the log alongside the workbook — useful for QC review before re-uploading below.
              </p>
            </div>
          )}

          {/* Post-QC re-upload: edit Cleaned Output in Excel, re-upload here,
              receive a zip of per-category CSVs.  Submits to the standalone
              endpoint so it isn't blocked by the original Phase 2 run being
              evicted from the registry. */}
          {downloadUrl && !postQcDownloadUrl && (
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
                disabled={!postQcFile || postQcSubmitting || !!postQcRunId}
                onClick={onPostQcUpload}
                className="btn-success"
              >
                {postQcSubmitting
                  ? "Uploading…"
                  : postQcRunId
                    ? (postQcStatus?.stage_label ?? "Processing…")
                    : "Finalise & Export"}
              </button>
              {postQcStatus && postQcStatus.state === "error" && (
                <div className="text-xs text-red-700">
                  {postQcStatus.error_title ?? "Post-QC failed"}
                  {postQcStatus.error_advice ? ` — ${postQcStatus.error_advice}` : ""}
                </div>
              )}
            </div>
          )}

          {postQcDownloadUrl && (
            <div className="rounded-2xl border border-emerald-200 bg-emerald-50/80 backdrop-blur text-emerald-900 p-5 mb-4 fade-in-up">
              <div className="font-medium mb-2">
                Post-QC export ready —{" "}
                {postQcStatus?.post_qc_categories?.length ?? 0} categor
                {(postQcStatus?.post_qc_categories?.length ?? 0) === 1 ? "y" : "ies"}
              </div>
              <p className="text-xs text-emerald-900/70 mb-3">
                Workflow complete — clicking download will start the file and reset this page.
              </p>
              {/* Final download — Phase 3 zip already bundles per-category
                  CSVs + the run log (output_QClogs.txt).  After the click
                  the workflow is genuinely done; auto-reset wipes server
                  tmpdirs and clears the page so analysts don't linger on
                  a stale screen. */}
              <a
                href={api.downloadUrl(postQcDownloadUrl)}
                onClick={finishAndReset}
                className="btn-primary inline-flex items-center"
              >
                Download AIC_Phase2_3_exports.zip
              </a>
            </div>
          )}

          {/* Logs collapsed by default — same disclosure pattern as the
              Phase 1 page so the run UI stays focused on stage + progress.
              The dot in the summary picks up the run's current state. */}
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
                    : isPaused ? "bg-amber-500"
                    : isRunning ? "bg-brand-500"
                    : "bg-emerald-500"
                  }`} />
                  <span>
                    Pipeline output
                    <span className="text-zinc-400"> · </span>
                    <span className="tabular-nums">{status.log_cursor}</span> log lines
                  </span>
                </span>
                <span className="text-zinc-400 group-open:hidden">Show ▾</span>
                <span className="text-zinc-400 hidden group-open:inline">Hide ▴</span>
              </summary>
              {/* Mount-on-open: FullLogTail fetches the whole buffer, not
                  just the live 60-line tail in the status response.  This
                  is what the analyst needs for QC of the cleaned output. */}
              <div className="mt-3">
                {logsOpen && (
                  <FullLogTail runId={runId} active={isRunning || isPaused} />
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
