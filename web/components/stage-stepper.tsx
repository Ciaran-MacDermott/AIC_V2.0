"use client";

import type { JobState } from "@/lib/types";

/**
 * Horizontal stage indicator — mirrors the Streamlit `_stage_stepper`
 * helper at lines 459-493 of pages/2_Phase_3_Pipeline_and_QC.py.
 *
 * Each step represents one of the JobState values the worker passes
 * through.  The stepper highlights the active step in brand purple,
 * shows already-completed steps in zinc, and dims future steps.
 */

const PHASE2_STEPS: { key: JobState | "config"; label: string }[] = [
  { key: "running",          label: "1  Processing" },
  { key: "mismatch_pending", label: "2  Mismatch Review" },
  { key: "done",             label: "3  Cleaned Output QC" },
  { key: "post_qc_running",  label: "4  Re-collapse" },
  { key: "post_qc_done",     label: "5  Export" },
];

const PHASE2_PROGRESSION: Record<string, number> = {
  queued:            0,
  running:           0,
  mismatch_pending:  1,
  done:              2,
  post_qc_running:   3,
  post_qc_done:      4,
};

export function StageStepper({
  state,
  steps = PHASE2_STEPS,
}: {
  state: JobState;
  steps?: { key: string; label: string }[];
}) {
  const activeIndex = PHASE2_PROGRESSION[state] ?? 0;

  return (
    <div className="flex items-center flex-wrap gap-1 mb-4 text-xs">
      {steps.map((step, i) => {
        const status = i < activeIndex ? "done" : i === activeIndex ? "active" : "pending";
        const colour =
          status === "active" ? "text-brand-700 font-semibold" :
          status === "done"   ? "text-ok"                       :
                                "text-zinc-400 font-normal";
        return (
          <span key={step.key} className="flex items-center gap-1">
            <span className={colour}>{step.label}</span>
            {i < steps.length - 1 && (
              <span className="text-zinc-300 mx-1">›</span>
            )}
          </span>
        );
      })}
    </div>
  );
}
