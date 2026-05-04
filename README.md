# AIC — FastAPI + Next.js refactor

Replacement UI for the Streamlit pages in
[Ciaran-MacDermott/AIC_PHASE1](https://github.com/Ciaran-MacDermott/AIC_PHASE1):
both Phase 1 (`1_Phase_1_Attribute_Mapping.py`) and Phase 2 + 3
(`pages/2_Phase_3_Pipeline_and_QC.py`).

The pipeline code (`ml_package/`, `phase3_package/`, `aic_utils.py`) is
vendored from the upstream `main` branch and **not modified** — when
upstream changes, copy the package back over.

## Layout

```
refactor/
├── api/                    # FastAPI BFF
│   ├── jobs.py               # in-memory job registry, global pipeline lock, log buffer
│   ├── pipeline.py           # Phase 1 adapter — calls into ml_package
│   ├── pipeline_phase2.py    # Phase 2/3 adapter — calls into phase3_package
│   ├── qc_view.py            # server-side QC sheet shaping
│   ├── worker.py             # background threads (Phase 1 and Phase 2)
│   ├── schemas.py            # Pydantic models (mirrored in web/lib/types.ts)
│   └── main.py               # routes
├── ml_package/             # vendored from AIC_PHASE1@main — Phase 1 ML code
├── phase3_package/         # vendored — Phase 2/3 algorithm code
├── aic_utils.py            # vendored
├── web/                    # Next.js 15 + Tailwind + ag-grid
│   ├── app/
│   │   ├── page.tsx          # Phase 1 (upload + run + progress)
│   │   ├── qc/page.tsx       # QC wizard (?runId=…)
│   │   └── phase2/page.tsx   # Phase 2/3 (zip upload + mismatch review)
│   ├── components/
│   │   ├── qc-grid.tsx
│   │   ├── mismatch-form.tsx
│   │   └── …
│   └── lib/
├── tests/
│   ├── test_jobs.py        ┐
│   ├── test_qc_view.py     │  fast unit tests — heavy ML deps stubbed
│   ├── test_api.py         │  in conftest, suite runs in <0.5s
│   ├── test_phase2_api.py  ┘
│   └── integration/
│       └── test_phase1_real.py  # end-to-end through real ml_package
└── requirements.txt
```

## Local dev

Python BFF (terminal 1):

```bash
cd refactor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn api.main:app --reload --port 8000
```

Next.js frontend (terminal 2):

```bash
cd refactor/web
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Open <http://localhost:3000>.

## Production build

`output: "export"` in `next.config.mjs` produces a static folder. The
FastAPI app mounts `web/out/` at `/` so everything runs on a single
port:

```bash
cd refactor/web && npm run build
cd .. && uvicorn api.main:app --port 8000
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Two layers:

- **Fast tests** (`tests/test_*.py`) — run in <0.5s. The fast-test
  conftest stubs `ml_package` so no XGBoost / NLTK / openpyxl is
  required; covers the registry, QC sheet shaping, every Phase 1 +
  Phase 2 route including the full mismatch-pause/resume state machine.
- **Integration test** (`tests/integration/test_phase1_real.py`) —
  drives the real FastAPI BFF through the real `ml_package` pipeline
  with a synthetic xlsx + csv fixture: upload → background pipeline
  (lookup + BM25 + XGBoost + ensemble) → QC wizard → finalize →
  download. Skipped automatically if heavy ML deps aren't installed.

35 tests, all passing.

## Architecture notes

- **Background jobs, not sync POSTs.** A Phase 1 run takes minutes; the
  browser kicks off `POST /api/phase1/runs`, gets a `run_id`, and polls
  `GET /api/runs/{id}` for status + a tail of the log every ~700ms.
- **Single-tenant pipeline.** `api/jobs.py::PIPELINE_LOCK` serialises
  pipeline execution because the legacy code mutates `sys.path` /
  `os.chdir` inside ml_package; concurrent runs would clobber each
  other. Subprocess isolation is the right v2 fix.
- **QC sheet payload is self-contained.** The server pre-computes
  `row_flags` (priority, low-score) and ships `original_values` so the
  React grid does cell colouring + edit detection client-side without a
  round-trip per keystroke. Saves are diff-only, debounced 600ms.
- **One job record per phase.** Phase 1 ends at `done` once the QC
  xlsx is materialised; a Phase 2 run is a new record. State machines
  (and inputs and artifacts) differ enough that conflating them
  forced shared semantics that didn't fit.
- **Mismatch pause is a `threading.Event`.** When Phase A surfaces
  BRAND vs TOOL_BRAND mismatches the worker attaches a JSON-safe
  payload to the JobRecord and blocks on `resume_event.wait()`. The
  `/api/runs/{id}/mismatch/resolve` route writes corrections onto the
  record then sets the event so Phase B picks up exactly where Phase
  A left off.
