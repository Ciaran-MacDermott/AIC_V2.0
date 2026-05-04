"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { listRecent, forgetRun, type RecentRun } from "@/lib/recent";
import type { ActiveRunSummary, JobState } from "@/lib/types";

const ACTIVE_POLL_MS = 10000;

const STATE_TONE: Record<JobState, string> = {
  queued:           "bg-zinc-100 text-zinc-600",
  running:          "bg-brand-50 text-brand-700",
  qc_ready:         "bg-amber-50 text-amber-700",
  finalizing:       "bg-brand-50 text-brand-700",
  done:             "bg-emerald-50 text-emerald-700",
  error:            "bg-red-50 text-red-700",
  stopped:          "bg-zinc-100 text-zinc-500",
  mismatch_pending: "bg-amber-50 text-amber-700",
  post_qc_running:  "bg-brand-50 text-brand-700",
  post_qc_done:     "bg-emerald-50 text-emerald-700",
};

function StateChip({ state }: { state: JobState }) {
  return (
    <span className={`rounded-full px-2 py-0.5 text-[10px] uppercase tracking-wide ${STATE_TONE[state]}`}>
      {state.replace(/_/g, " ")}
    </span>
  );
}

function deepLinkFor(phase: "phase1" | "phase2", runId: string): string {
  return phase === "phase1"
    ? `/?runId=${encodeURIComponent(runId)}`
    : `/phase2?runId=${encodeURIComponent(runId)}`;
}

export function RunsSidebar({ currentRunId }: { currentRunId?: string | null }) {
  const [active, setActive] = useState<ActiveRunSummary[]>([]);
  const [recent, setRecent] = useState<RecentRun[]>([]);
  const [collapsed, setCollapsed] = useState(false);
  const [activeError, setActiveError] = useState(false);

  // Active runs from server — refreshed on a slow poll.  We don't piggyback
  // on the page's status poll because the page may not be polling at all
  // (e.g. when the user lands fresh and hasn't started a run yet).
  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;

    async function tick() {
      try {
        const { runs } = await api.listRuns();
        if (cancelled) return;
        setActive(runs);
        setActiveError(false);
      } catch {
        if (cancelled) return;
        setActiveError(true);
      } finally {
        if (!cancelled) timer = window.setTimeout(tick, ACTIVE_POLL_MS);
      }
    }

    tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  // Recent runs from localStorage — refresh whenever the page or another
  // component records or forgets a run.
  useEffect(() => {
    function refresh() { setRecent(listRecent()); }
    refresh();
    window.addEventListener("aic:recent-runs", refresh);
    window.addEventListener("storage", refresh);   // sibling tab updates
    return () => {
      window.removeEventListener("aic:recent-runs", refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  const activeIds = new Set(active.map((r) => r.run_id));
  // De-dupe recent against active so we don't render the same run twice;
  // the active panel always wins because its state is fresher.
  const recentOnly = recent.filter((r) => !activeIds.has(r.run_id));

  if (active.length === 0 && recentOnly.length === 0 && !activeError) {
    // Nothing to show yet — keep the page chrome clean.
    return null;
  }

  return (
    <aside className="surface-card mb-4">
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="w-full flex items-center justify-between px-4 py-2 text-xs font-medium text-zinc-600 hover:bg-zinc-50 rounded-t-xl"
      >
        <span>
          Runs · {active.length} active{recentOnly.length ? ` · ${recentOnly.length} recent` : ""}
        </span>
        <span className="text-zinc-400">{collapsed ? "▸" : "▾"}</span>
      </button>

      {!collapsed && (
        <div className="px-4 pb-3 pt-1 text-xs">
          {activeError && (
            <p className="text-zinc-400 italic">Couldn&apos;t reach the server.</p>
          )}

          {active.length > 0 && (
            <div className="space-y-1.5">
              <p className="text-[10px] uppercase tracking-wide text-zinc-400">On the server</p>
              {active.map((r) => (
                <RunRow
                  key={r.run_id}
                  href={deepLinkFor(r.phase, r.run_id)}
                  isCurrent={r.run_id === currentRunId}
                  state={r.state}
                  phase={r.phase}
                  label={r.stage_label || r.run_id.slice(0, 6)}
                  detail={`run ${r.run_id.slice(0, 6)} · ${Math.round(r.elapsed_s)}s`}
                />
              ))}
            </div>
          )}

          {recentOnly.length > 0 && (
            <div className="mt-3 space-y-1.5">
              <p className="text-[10px] uppercase tracking-wide text-zinc-400">Recent on this device</p>
              {recentOnly.map((r) => (
                <RunRow
                  key={r.run_id}
                  href={deepLinkFor(r.phase, r.run_id)}
                  isCurrent={r.run_id === currentRunId}
                  state={r.last_state ?? "stopped"}
                  phase={r.phase}
                  label={r.label || r.run_id.slice(0, 6)}
                  detail={new Date(r.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                  onForget={() => { forgetRun(r.run_id); setRecent(listRecent()); }}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </aside>
  );
}

function RunRow({
  href, isCurrent, state, phase, label, detail, onForget,
}: {
  href: string;
  isCurrent: boolean;
  state: JobState;
  phase: "phase1" | "phase2";
  label: string;
  detail: string;
  onForget?: () => void;
}) {
  return (
    <div className={`flex items-center gap-2 rounded-md px-2 py-1 ${isCurrent ? "bg-brand-50" : "hover:bg-zinc-50"}`}>
      <StateChip state={state} />
      <span className="text-[10px] uppercase text-zinc-400 tracking-wide">{phase === "phase1" ? "P1" : "P2"}</span>
      <Link href={href} className="flex-1 truncate text-zinc-700 hover:text-brand-700">
        {label}
      </Link>
      <span className="text-zinc-400 tabular-nums">{detail}</span>
      {onForget && (
        <button
          type="button"
          onClick={onForget}
          title="Forget this run"
          className="text-zinc-300 hover:text-zinc-600 px-1"
        >
          ×
        </button>
      )}
    </div>
  );
}
