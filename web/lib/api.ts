// Fetch wrapper for the FastAPI BFF. One typed callable per backend route.
// In dev: NEXT_PUBLIC_API_URL=http://localhost:8000 (Next on :3000, BFF on :8000).
// In prod: empty string — same origin as the static frontend served by FastAPI.

import type {
  JobStatus,
  LogChunk,
  MismatchPayload,
  MismatchResolve,
  Phase2Config,
  Phase2Done,
  Phase2ScanResult,
  QcEditPayload,
  QcFinalized,
  QcSheetList,
  QcSheetPayload,
  RunCreated,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

async function postJSON<T>(path: string, body?: unknown, method = "POST"): Promise<T> {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const res = await fetch(`${API_BASE}${path}`, init);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  startPhase1:   async (xlsx: File, csv: File): Promise<RunCreated> => {
    const fd = new FormData();
    fd.append("xlsx", xlsx);
    fd.append("csv", csv);
    const res = await fetch(`${API_BASE}/api/phase1/runs`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  },

  startPhase1FromZip: async (zipFile: File): Promise<RunCreated> => {
    const fd = new FormData();
    fd.append("zip", zipFile);
    const res = await fetch(`${API_BASE}/api/phase1/runs/zip`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  },

  status:        (id: string) => getJSON<JobStatus>(`/api/runs/${id}`),
  logsSince:     (id: string, since: number) =>
    getJSON<LogChunk>(`/api/runs/${id}/logs?since=${since}`),
  stop:          (id: string) => postJSON<void>(`/api/runs/${id}/stop`),
  remove:        (id: string) => postJSON<void>(`/api/runs/${id}`, undefined, "DELETE"),

  qcSheets:      (id: string) => getJSON<QcSheetList>(`/api/runs/${id}/qc/sheets`),
  qcSheet:       (id: string, key: string) =>
    getJSON<QcSheetPayload>(`/api/runs/${id}/qc/sheets/${encodeURIComponent(key)}`),
  qcSave:        (id: string, key: string, body: QcEditPayload) =>
    postJSON<void>(`/api/runs/${id}/qc/sheets/${encodeURIComponent(key)}`, body, "PUT"),
  qcFinalize:    (id: string) => postJSON<QcFinalized>(`/api/runs/${id}/qc/finalize`),

  startPhase2:   async (zipFile: File, config: Phase2Config): Promise<RunCreated> => {
    const fd = new FormData();
    fd.append("zip", zipFile);
    fd.append("config", JSON.stringify(config));
    const res = await fetch(`${API_BASE}/api/phase2/runs`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  },

  startPhase2FromParent: async (
    parentRunId: string, config: Phase2Config,
  ): Promise<RunCreated> => {
    const fd = new FormData();
    fd.append("config", JSON.stringify(config));
    const res = await fetch(
      `${API_BASE}/api/phase2/runs/from-parent/${encodeURIComponent(parentRunId)}`,
      { method: "POST", body: fd },
    );
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  },

  scanPhase2Zip: async (zipFile: File): Promise<Phase2ScanResult> => {
    const fd = new FormData();
    fd.append("zip", zipFile);
    const res = await fetch(`${API_BASE}/api/phase2/scan`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  },

  scanPhase2FromParent: (parentRunId: string) =>
    getJSON<Phase2ScanResult>(
      `/api/phase2/scan/from-parent/${encodeURIComponent(parentRunId)}`,
    ),

  // Standalone post-QC creates a fresh run for the edited xlsx, so it
  // survives idle-eviction or BFF restarts of the parent Phase 2 run.
  postQcStandalone: async (xlsx: File, isCustomCollapse: boolean): Promise<RunCreated> => {
    const fd = new FormData();
    fd.append("xlsx", xlsx);
    fd.append("is_custom_collapse", String(isCustomCollapse));
    const res = await fetch(`${API_BASE}/api/post_qc/standalone`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  },

  mismatch:        (id: string) => getJSON<MismatchPayload>(`/api/runs/${id}/mismatch`),
  resolveMismatch: (id: string, body: MismatchResolve) =>
    postJSON<Phase2Done>(`/api/runs/${id}/mismatch/resolve`, body),

  downloadUrl:   (path: string) => `${API_BASE}${path}`,
};
