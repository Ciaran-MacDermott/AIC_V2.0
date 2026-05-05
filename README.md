# AIC — FastAPI + Next.js (V2)

Replacement UI for the Streamlit pages in
[Ciaran-MacDermott/AIC_PHASE1](https://github.com/Ciaran-MacDermott/AIC_PHASE1):
both Phase 1 (`1_Phase_1_Attribute_Mapping.py`) and Phase 2 + 3
(`pages/2_Phase_3_Pipeline_and_QC.py`).

The pipeline code (`ml_package/`, `phase3_package/`, `aic_utils.py`) is
vendored from the upstream `main` branch and **not modified** — when
upstream changes, copy the package back over.

Production deployment is targeted at an internal Circana workstation
behind VPN + SSO, ~15 analysts, walled-garden (offline) install. See
the architecture notes at the bottom for what that constrains.

## Layout

```
v2/
├── api/                    # FastAPI BFF
│   ├── _nltk_bootstrap.py    # walled-garden NLTK setup (path + no-op download)
│   ├── errors.py             # exception → user-facing title/advice/category
│   ├── inputs.py             # zip/xlsx scanning, Phase 2 column metadata
│   ├── jobs.py               # in-memory JobRegistry, RUN_SLOTS, idle-TTL reaper
│   ├── pipeline.py           # Phase 1 adapter — calls into ml_package
│   ├── pipeline_phase2.py    # Phase 2/3 adapter — calls into phase3_package
│   ├── qc_view.py            # server-side QC sheet shaping
│   ├── run_pipeline.py       # subprocess CLI entry — spawned per run
│   ├── worker.py             # background threads + subprocess orchestration
│   ├── schemas.py            # Pydantic models (mirrored in web/lib/types.ts)
│   └── main.py               # routes
├── ml_package/             # vendored from AIC_PHASE1@main — Phase 1 ML code
├── phase3_package/         # vendored — Phase 2/3 algorithm code
├── aic_utils.py            # vendored
├── web/                    # Next.js 15 + Tailwind + ag-grid
│   ├── app/
│   │   ├── page.tsx          # Phase 1 (upload + run + progress)
│   │   ├── qc/page.tsx       # QC wizard (?runId=…)
│   │   └── phase2/page.tsx   # Phase 2/3 (zip upload + mismatch review + post-QC)
│   ├── components/
│   │   ├── qc-grid.tsx
│   │   ├── mismatch-form.tsx
│   │   ├── phase2-advanced.tsx
│   │   ├── log-tail.tsx       # FullLogTail lazy-fetches the whole buffer
│   │   └── …
│   └── lib/                  # api client + Pydantic-mirrored types
├── tests/
│   ├── test_jobs.py        ┐
│   ├── test_qc_view.py     │  fast unit tests — heavy ML deps stubbed
│   ├── test_api.py         │  in conftest, suite runs in <1s
│   ├── test_phase2_api.py  │
│   ├── test_post_qc.py     │
│   └── …                   ┘
│   └── integration/
│       └── test_phase1_real.py  # end-to-end through real ml_package
└── requirements.txt
```

## Local dev (Windows / PowerShell or git-bash)

Python BFF (terminal 1):

```bash
cd v2
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
# --reload-dir is important — bare --reload watches the cwd, which can
# silently fail to detect api/main.py edits if you launched from elsewhere.
.venv/Scripts/python -m uvicorn api.main:app --port 8000 --reload --reload-dir api
```

Next.js frontend (terminal 2):

```bash
cd v2/web
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Open <http://localhost:3000>.

## Production build (single-port)

`output: "export"` in `next.config.mjs` produces a static folder. The
FastAPI app mounts `web/out/` at `/` so the whole app runs on a single
port:

```bash
cd v2/web && npm run build
cd .. && .venv/Scripts/python -m uvicorn api.main:app --port 8000
```

Walled-garden notes:
- NLTK corpora are bundled under `nltk_data/` and surfaced via
  `_nltk_bootstrap.py` — `nltk.download()` is no-op'd at import time.
- All Python deps must be wheel-installable from the bundled mirror;
  no runtime network calls.
- See `feedback_walled_garden.md` in the project memory for the
  build-vs-runtime distinction.

## Tests

```bash
.venv/Scripts/python -m pytest tests/ -v
```

Two layers:

- **Fast tests** (`tests/test_*.py`, ~105 tests) — run in <1s. The
  fast-test conftest stubs `ml_package` so no XGBoost / NLTK /
  openpyxl is required; covers the registry, QC sheet shaping, every
  Phase 1 + Phase 2 route, the mismatch pause/resume state machine,
  the standalone post-QC flow, and the NLTK bootstrap. The conftest
  sets `AIC_INPROCESS=1` so the worker calls the (stubbed) pipeline
  in-process instead of spawning a subprocess — that's how the route
  monkeypatches in these tests stay effective.
- **Integration test** (`tests/integration/test_phase1_real.py`) —
  drives the real FastAPI BFF through the real `ml_package` pipeline
  with a synthetic xlsx + csv fixture: upload → background pipeline
  (lookup + BM25 + XGBoost + ensemble) → QC wizard → finalize →
  download. Skipped automatically if heavy ML deps aren't installed.

## Architecture notes

- **Background jobs, not sync POSTs.** A Phase 1 run takes minutes;
  the browser kicks off `POST /api/phase1/runs`, gets a `run_id`, and
  polls `GET /api/runs/{id}` for status + a tail of the log every
  ~700ms.
- **Subprocess-isolated runs, capped at 5.** Each pipeline stage
  spawns `python -m api.run_pipeline {phase1|phase2_a|phase2_b|post_qc}`
  in its own process so concurrent runs can't trample each other via
  `ml_package`'s `chdir` / `sys.path` mutations. Concurrency is gated
  by a `BoundedSemaphore` (`RUN_SLOTS`, default 5); runs beyond that
  queue at `state=queued` with an ETA chip in the UI.
- **In-memory JobRegistry with idle-TTL eviction.** Records evict 60
  min after their last touch; a background reaper thread sweeps even
  when the API is idle. `last_touched` is refreshed by both worker
  progress and any API touch, so a long QC review keeps the run
  alive.
- **QC sheet payload is self-contained.** The server pre-computes
  `row_flags` (priority, low-score) and ships `original_values` so
  the React grid does cell colouring + edit detection client-side
  without a round-trip per keystroke. Saves are diff-only, debounced
  600ms.
- **Standalone post-QC.** `POST /api/post_qc/standalone` accepts the
  edited xlsx and creates its own JobRecord — no dependency on the
  Phase 2 run still being in the registry. Survives BFF restarts and
  idle-TTL eviction of the parent run.
- **Workflow-end auto-cleanup.** When the analyst clicks the truly
  final download (Phase 1 QC xlsx or Phase 3 post-QC zip), the page
  resets and the relevant runs are deleted server-side — `tmpdir`
  rmtree'd, registry entry popped — so resources come back without
  waiting for the idle reaper.
- **One job record per phase.** Phase 1 ends at `done` once the QC
  xlsx is materialised; a Phase 2 run is a new record (linked via
  `parent_run_id`). State machines, inputs, and artifacts differ
  enough that conflating them forced shared semantics that didn't
  fit.
- **Mismatch pause is a `threading.Event` with a 1h cap.** When
  Phase A surfaces BRAND vs TOOL_BRAND mismatches the worker
  attaches a JSON-safe payload to the JobRecord and blocks on
  `resume_event.wait(timeout=…)`. The
  `/api/runs/{id}/mismatch/resolve` route writes corrections onto
  the record then sets the event so Phase B picks up exactly where
  Phase A left off. Reviews abandoned past 1h flip the run to
  `stopped` so the registry slot frees.
- **Walled-garden encoding.** Subprocess stdio and any `Path.write_text`
  are pinned to UTF-8 — Windows defaults to `cp1252`, which the
  pipeline's checkmark/arrow glyphs would otherwise crash on.
