// Recent-runs registry kept in localStorage so an analyst who closes
// their tab can pick a run back up — and so multiple browser sessions
// on the same workstation share visibility into "what did I just run".

import type { JobState, Phase } from "./types";

export type RecentRun = {
  run_id: string;
  phase: Phase;
  created_at: number;       // ms epoch — when we first saw the run client-side
  last_state?: JobState;    // best-effort; refreshed on each status poll we observe
  label?: string;           // friendly name (e.g. uploaded zip filename)
  parent_run_id?: string;
};

const KEY = "aic.recent.runs";
const MAX = 12;

function read(): RecentRun[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function write(rows: RecentRun[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(KEY, JSON.stringify(rows.slice(0, MAX)));
    // Lets sibling components in the same tab refresh without a custom bus.
    window.dispatchEvent(new Event("aic:recent-runs"));
  } catch {
    // Quota / private mode — silently degrade.
  }
}

export function recordRun(entry: RecentRun): void {
  const existing = read();
  const idx = existing.findIndex((r) => r.run_id === entry.run_id);
  if (idx >= 0) {
    existing[idx] = { ...existing[idx], ...entry };
  } else {
    existing.unshift(entry);
  }
  write(existing);
}

export function updateRunState(run_id: string, state: JobState): void {
  const existing = read();
  const idx = existing.findIndex((r) => r.run_id === run_id);
  if (idx < 0) return;
  if (existing[idx].last_state === state) return;
  existing[idx] = { ...existing[idx], last_state: state };
  write(existing);
}

export function listRecent(): RecentRun[] {
  return read();
}

export function forgetRun(run_id: string): void {
  write(read().filter((r) => r.run_id !== run_id));
}
