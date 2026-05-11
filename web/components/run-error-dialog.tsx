"use client";

// Error banner for failed runs. Picks tone + label from the worker's
// classified error_category (input/config/server). Falls back to "Server
// error" for transport-level failures that never reached the classifier.

import { useState } from "react";
import { api } from "@/lib/api";
import type { JobStatus } from "@/lib/types";

// input and config share the amber tone — both are "your fix", not "ours".
const CATEGORY_TONE: Record<NonNullable<JobStatus["error_category"]>, {
  border: string; bg: string; pill: string; pillText: string;
}> = {
  input:  { border: "border-amber-200",   bg: "bg-amber-50",   pill: "bg-amber-100",   pillText: "text-amber-800"   },
  config: { border: "border-amber-200",   bg: "bg-amber-50",   pill: "bg-amber-100",   pillText: "text-amber-800"   },
  server: { border: "border-red-200",     bg: "bg-red-50",     pill: "bg-red-100",     pillText: "text-red-800"     },
};

const CATEGORY_LABEL: Record<NonNullable<JobStatus["error_category"]>, string> = {
  input:  "Input issue",
  config: "Configuration issue",
  server: "Server error",
};

export function RunErrorDialog({
  status, onRetry,
}: {
  status: JobStatus;
  onRetry?: () => void;
}) {
  const [showTechnical, setShowTechnical] = useState(false);
  const tone = CATEGORY_TONE[status.error_category ?? "server"];
  const label = CATEGORY_LABEL[status.error_category ?? "server"];

  // Fall back to the raw error string if the worker didn't classify the
  // failure (older runs or transport-level errors before the worker ran).
  const title = status.error_title ?? "Run failed";
  const advice = status.error_advice ?? (status.error
    ? "The pipeline reported an error. See technical detail below."
    : "The run finished in an error state.");

  return (
    <div className={`rounded-xl border ${tone.border} ${tone.bg} p-5 mb-4`}>
      <div className="flex items-start gap-3">
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] uppercase tracking-wide ${tone.pill} ${tone.pillText}`}>
          {label}
        </span>
        <div className="flex-1">
          <h3 className="font-medium text-zinc-900">{title}</h3>
          <p className="mt-1 text-sm text-zinc-700">{advice}</p>

          <div className="mt-3 flex flex-wrap items-center gap-3">
            {onRetry && (
              <button
                type="button"
                onClick={onRetry}
                className="rounded-md bg-zinc-900 hover:bg-zinc-700 text-white text-sm font-medium px-3 py-1.5 transition-colors"
              >
                Start over
              </button>
            )}
            <a
              href={api.downloadUrl(`/api/runs/${status.run_id}/artifacts/log.txt`)}
              className="text-sm text-zinc-600 hover:text-zinc-900 underline"
            >
              Download log
            </a>
            {status.error && (
              <button
                type="button"
                onClick={() => setShowTechnical((v) => !v)}
                className="text-sm text-zinc-500 hover:text-zinc-700 underline"
              >
                {showTechnical ? "Hide" : "Show"} technical detail
              </button>
            )}
          </div>

          {showTechnical && status.error && (
            <pre className="mt-3 max-h-64 overflow-auto rounded-md bg-zinc-900 text-zinc-100 text-xs p-3 whitespace-pre-wrap">
              {status.error}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
