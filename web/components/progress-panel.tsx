"use client";

// Pipeline progress card — stage label, elapsed time, queue chip (while
// queued), download-log link, and a state-coloured progress bar.

import type { JobStatus } from "@/lib/types";

function fmtElapsed(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

function fmtEta(s: number): string {
  if (s < 60) return `~${Math.round(s)}s`;
  const m = Math.round(s / 60);
  return `~${m} min`;
}

export function ProgressPanel({ status }: { status: JobStatus }) {
  const {
    state, progress, stage_label, elapsed_s,
    queue_position, queue_depth, eta_seconds,
  } = status;
  const isError = state === "error";
  const isStopped = state === "stopped";
  const isQueued = state === "queued";
  const pct = Math.round(progress * 100);

  return (
    <div className="surface-card p-6 mb-4">
      {/* Queue chip — only visible while waiting for PIPELINE_LOCK. Gives
          analysts a meaningful "what am I waiting for" instead of a
          spinner with no signal during high-concurrency periods. */}
      {isQueued && queue_position != null && (
        <div className="mb-3 flex items-center gap-2 text-xs">
          <span className="rounded-full bg-brand-50 text-brand-700 border border-brand-200 px-2 py-0.5">
            Queued — position {queue_position + 1}
            {queue_depth ? ` of ${queue_depth}` : ""}
          </span>
          {eta_seconds != null && (
            <span className="text-zinc-500">
              {fmtEta(eta_seconds)} based on recent runs
            </span>
          )}
        </div>
      )}

      <div className="flex items-center justify-between mb-3 gap-3">
        <span className="text-sm font-medium text-zinc-700">{stage_label || "Starting…"}</span>
        <span className="text-xs text-zinc-500 tabular-nums">{fmtElapsed(elapsed_s)}</span>
      </div>

      {isError ? (
        <div className="h-1 rounded-full bg-err" />
      ) : isStopped ? (
        <div className="h-1 rounded-full bg-zinc-300" />
      ) : (
        <div className="h-2 rounded-full bg-brand-100 overflow-hidden">
          <div
            className="h-full bg-brand-600 transition-all duration-300"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </div>
  );
}
