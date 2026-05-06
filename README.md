# AIC — FastAPI + Next.js (V2)

Replacement UI for the Streamlit pages in
[Ciaran-MacDermott/AIC_PHASE1](https://github.com/Ciaran-MacDermott/AIC_PHASE1):
both Phase 1 (`1_Phase_1_Attribute_Mapping.py`) and Phase 2 + 3
(`pages/2_Phase_3_Pipeline_and_QC.py`).

The pipeline code (`ml_package/`, `phase3_package/`, `aic_utils.py`) is
vendored from the upstream `main` branch and is not modified — when
upstream changes, copy the package back over.

Production target is an internal Circana workstation behind VPN + SSO,
~15 analysts, air-gapped install. The Dockerfile in the
repo root produces the deployable image.

## Layout

```
├── api/                    # FastAPI BFF
│   ├── _nltk_bootstrap.py    # air-gapped NLTK setup (path + no-op download)
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
├── nltk_data/              # bundled NLTK corpora (stopwords, punkt, punkt_tab) — loaded by api/_nltk_bootstrap.py
├── web/                    # Next.js 15 + Tailwind + ag-grid
│   ├── app/
│   │   ├── page.tsx          # Phase 1 (upload + run + progress)
│   │   ├── qc/page.tsx       # QC wizard (?runId=…)
│   │   └── phase2/page.tsx   # Phase 2/3 (zip upload + mismatch review + post-QC)
│   ├── components/           # qc-grid, mismatch-form, phase2-advanced, log-tail, …
│   └── lib/                  # api client + Pydantic-mirrored types
├── tests/                  # fast unit suite + integration
└── requirements.txt
```

## Local development

Python BFF (terminal 1):

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn api.main:app --port 8000 --reload --reload-dir api
```

`--reload-dir api` is required so the watcher sees changes regardless
of the directory uvicorn was launched from.

Next.js frontend (terminal 2):

```bash
cd web
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Open <http://localhost:3000>.

## Production build (single-port)

`output: "export"` in `next.config.mjs` produces a static folder. The
FastAPI app mounts `web/out/` at `/`, so the whole app runs on a single
port:

```bash
cd web && npm run build && cd ..
.venv/bin/python -m uvicorn api.main:app --port 8000
```

Air-gapped notes:

- The English NLTK corpora (`stopwords`, `punkt`, `punkt_tab`) are
  committed under `nltk_data/` and loaded by `api/_nltk_bootstrap.py`,
  which prepends the bundled directory to `nltk.data.path` and replaces
  `nltk.download()` with a no-op. The bootstrap runs from
  `api/__init__.py` so it primes NLTK before `ml_package` is imported,
  letting the vendored package stay an unmodified copy of upstream.
- All Python dependencies must be wheel-installable from the bundled
  mirror — no runtime network calls.

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Two layers:

- **Fast tests** (`tests/test_*.py`, ~105 tests) run in under a second.
  The fast-test conftest stubs `ml_package` so no XGBoost, NLTK, or
  openpyxl is required; coverage spans the registry, QC sheet shaping,
  every Phase 1 and Phase 2 route, the mismatch pause/resume state
  machine, the standalone post-QC flow, and the NLTK bootstrap. The
  conftest sets `AIC_INPROCESS=1` so the worker calls the (stubbed)
  pipeline in-process instead of spawning a subprocess, which is what
  keeps route monkeypatches effective.
- **Integration test** (`tests/integration/test_phase1_real.py`)
  drives the real FastAPI BFF through the real `ml_package` pipeline
  with a synthetic xlsx + csv fixture: upload → background pipeline
  (lookup + BM25 + XGBoost + ensemble) → QC wizard → finalize →
  download. Skipped automatically if heavy ML dependencies are not
  installed.

## Architecture notes

- **Background jobs, not synchronous POSTs.** A Phase 1 run takes
  minutes; the browser kicks off `POST /api/phase1/runs`, receives a
  `run_id`, and polls `GET /api/runs/{id}` for status and a tail of
  the log every ~700ms.
- **Subprocess-isolated runs, capped at 5.** Each pipeline stage
  spawns `python -m api.run_pipeline {phase1|phase2_a|phase2_b|post_qc}`
  in its own process, so concurrent runs cannot interfere via
  `ml_package`'s `chdir` / `sys.path` mutations. Concurrency is gated
  by a `BoundedSemaphore` (`RUN_SLOTS`, default 5); runs beyond that
  queue at `state=queued` with an ETA chip in the UI.
- **In-memory JobRegistry with idle-TTL eviction.** Records evict 60
  minutes after their last touch; a background reaper thread sweeps
  even when the API is idle. `last_touched` is refreshed by both
  worker progress and any API touch, so a long QC review keeps the
  run alive.
- **Self-contained QC sheet payload.** The server pre-computes
  `row_flags` (priority, low-score) and ships `original_values` so
  the React grid handles cell colouring and edit detection
  client-side without a round-trip per keystroke. Saves are diff-only
  and debounced at 600ms.
- **Standalone post-QC.** `POST /api/post_qc/standalone` accepts the
  edited xlsx and creates its own JobRecord — no dependency on the
  Phase 2 run still being in the registry. Survives BFF restarts and
  idle-TTL eviction of the parent run.
- **Workflow-end auto-cleanup.** When the analyst clicks the final
  download (Phase 1 QC xlsx or Phase 3 post-QC zip), the page resets
  and the relevant runs are deleted server-side — `tmpdir` rmtree'd,
  registry entry popped — so resources are released without waiting
  for the idle reaper.
- **One job record per phase.** Phase 1 ends at `done` once the QC
  xlsx is materialised; a Phase 2 run is a new record, linked via
  `parent_run_id`. State machines, inputs, and artifacts differ
  enough that conflating them would force shared semantics that did
  not fit either side.
- **Mismatch pause is a `threading.Event` with a 1h cap.** When
  Phase A surfaces BRAND vs TOOL_BRAND mismatches the worker
  attaches a JSON-safe payload to the JobRecord and blocks on
  `resume_event.wait(timeout=…)`. The
  `/api/runs/{id}/mismatch/resolve` route writes corrections onto
  the record and then sets the event, so Phase B picks up exactly
  where Phase A left off. Reviews abandoned past 1h flip the run to
  `stopped` and free the registry slot.
- **Air-gapped encoding.** Subprocess stdio and any
  `Path.write_text` calls are pinned to UTF-8 — Windows defaults to
  `cp1252`, which the pipeline's checkmark and arrow glyphs would
  otherwise crash on.
