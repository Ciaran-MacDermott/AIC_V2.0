"use client";

import { useEffect, useMemo, useRef } from "react";

const ERROR_RE = /(error|exception|traceback|runtimeerror)/i;
const WARN_RE  = /(^|\s)(warn|warning|missing|⚠)/i;

const COLOUR_RULES: { test: RegExp; cls: string }[] = [
  { test: ERROR_RE, cls: "text-err" },
  { test: WARN_RE,  cls: "text-warn" },
  { test: /(done|complete|written|filled)/i,          cls: "text-ok"   },
  { test: /(running|reading|building|writing|applying|start)/i, cls: "text-brand-700" },
];

function colourClass(line: string): string {
  for (const r of COLOUR_RULES) if (r.test.test(line)) return r.cls;
  return "text-zinc-700";
}

export function LogTail({ lines }: { lines: string[] }) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [lines]);

  // Mirrors the "⚠ N errors logged" warning the Streamlit page rendered
  // above the log box (line 798) — gives the analyst a nudge to look
  // before they otherwise scroll past several hundred lines of output.
  const counts = useMemo(() => {
    let errors = 0;
    let warnings = 0;
    for (const ln of lines) {
      if (ERROR_RE.test(ln)) errors++;
      else if (WARN_RE.test(ln)) warnings++;
    }
    return { errors, warnings };
  }, [lines]);

  return (
    <div className="space-y-1">
      {(counts.errors > 0 || counts.warnings > 0) && (
        <div className="flex items-center gap-2 text-xs">
          {counts.errors > 0 && (
            <span className="rounded-full bg-red-50 text-err border border-red-200 px-2 py-0.5">
              {counts.errors} error{counts.errors === 1 ? "" : "s"} logged
            </span>
          )}
          {counts.warnings > 0 && (
            <span className="rounded-full bg-amber-50 text-warn border border-amber-200 px-2 py-0.5">
              {counts.warnings} warning{counts.warnings === 1 ? "" : "s"}
            </span>
          )}
        </div>
      )}
      <div
        ref={ref}
        className="rounded-md border border-zinc-200 bg-zinc-50 px-4 py-3 font-mono text-xs leading-relaxed max-h-[420px] overflow-y-auto whitespace-pre-wrap"
      >
        {lines.length === 0 ? (
          <span className="text-zinc-400">Waiting for output…</span>
        ) : (
          lines.map((line, i) => (
            <div key={i} className={colourClass(line)}>
              {line}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
