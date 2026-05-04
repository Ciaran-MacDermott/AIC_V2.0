"use client";

import { useRef, useState, type DragEvent } from "react";

/**
 * File slot with a drag-and-drop affordance.  Drops only the first file
 * even when multiple are dropped — matches the underlying single-file
 * input contract on every upload route.
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

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
