"use client";

import { useMemo, useRef, useState, type DragEvent } from "react";

/**
 * File slot with a drag-and-drop affordance.  Drops only the first file
 * even when multiple are dropped — matches the underlying single-file
 * input contract on every upload route.
 *
 * A small client-side check on the picked file: extension must match the
 * ``accept`` list and the file must be non-empty.  The pipeline itself
 * does the deep validation (sheet names, required tool files, etc.) so
 * we don't duplicate that work pre-upload — we just want a quick "yes,
 * this looks like the right kind of file" tick.
 */
export function FileSlot({
  label,
  accept,
  file,
  onPick,
}: {
  label: string;
  accept: string;
  file: File | null;
  onPick: (f: File | null) => void;
}) {
  const ref = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);

  // Parse the accept string ("." prefixed extensions, comma-separated).
  // MIME types in accept are ignored — we only validate extensions.
  const allowedExts = useMemo(
    () => accept
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter((s) => s.startsWith(".")),
    [accept],
  );

  const validation = useMemo(() => checkFile(file, allowedExts), [file, allowedExts]);

  function onDragOver(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragging(true);
  }
  function onDragLeave() { setDragging(false); }
  function onDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files?.[0];
    if (f) onPick(f);
  }

  return (
    <div
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      className={[
        "group relative rounded-xl border bg-white/70 backdrop-blur p-4 transition-all",
        dragging
          ? "border-brand-500 bg-brand-50/70 ring-2 ring-brand-400/40"
          : file
            ? "border-zinc-200 hover:border-zinc-300"
            : "border-dashed border-zinc-300 hover:border-brand-400",
      ].join(" ")}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-medium text-zinc-800">{label}</div>
          <div className="text-xs text-zinc-500 mt-0.5 truncate">
            {file
              ? `${file.name} · ${formatSize(file.size)}`
              : dragging ? "Drop to upload" : "Click 'Choose…' or drop a file here"}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {file && validation && <ValidationBadge state={validation} />}
          {file && (
            <button
              type="button"
              onClick={() => onPick(null)}
              className="text-zinc-400 hover:text-zinc-700 text-xs px-1"
              title="Remove file"
              aria-label="Remove file"
            >
              ✕
            </button>
          )}
          <button
            type="button"
            onClick={() => ref.current?.click()}
            className="btn-base bg-brand-700 text-white hover:bg-brand-600 px-3 py-1.5 text-xs"
          >
            Choose…
          </button>
        </div>
      </div>
      <input
        ref={ref}
        type="file"
        accept={accept}
        className="hidden"
        onChange={(e) => onPick(e.target.files?.[0] ?? null)}
      />
    </div>
  );
}


type ValidationState = { ok: true } | { ok: false; reason: string };

function checkFile(file: File | null, allowedExts: string[]): ValidationState | null {
  if (!file) return null;
  if (file.size === 0) return { ok: false, reason: "File is empty" };
  if (allowedExts.length > 0) {
    const name = file.name.toLowerCase();
    if (!allowedExts.some((ext) => name.endsWith(ext))) {
      return { ok: false, reason: `Expected ${allowedExts.join(" or ")}` };
    }
  }
  return { ok: true };
}


function ValidationBadge({ state }: { state: ValidationState }) {
  if (state.ok) {
    return (
      <span
        className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 px-2.5 py-1 text-xs"
        title="File looks correct"
      >
        <svg viewBox="0 0 12 12" className="h-3.5 w-3.5 fill-none stroke-current" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M2.5 6.5 L5 9 L9.5 3.5" />
        </svg>
        file(s) ok
      </span>
    );
  }
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full bg-red-50 text-red-700 border border-red-200 px-2.5 py-1 text-xs max-w-[16rem] truncate"
      title={state.reason}
    >
      <svg viewBox="0 0 12 12" className="h-3.5 w-3.5 fill-none stroke-current" strokeWidth="2" strokeLinecap="round">
        <path d="M3 3 L9 9 M9 3 L3 9" />
      </svg>
      {state.reason}
    </span>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
