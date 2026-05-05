# syntax=docker/dockerfile:1.6

# ── Stage 1: build the Next.js static export ──────────────────────────────
FROM node:20-alpine AS web-build
WORKDIR /web

# Install dependencies first so package.json edits don't bust the cache.
COPY web/package.json web/package-lock.json ./
RUN npm ci

# Build the static export — produces /web/out/.
COPY web/ ./
RUN npm run build


# ── Stage 2: Python runtime, FastAPI serves the static export ────────────
FROM python:3.11-slim
WORKDIR /app

# curl: powers the HEALTHCHECK below.
# tini: minimal init that reaps zombie pipeline subprocesses + forwards
#   signals — uvicorn-as-PID-1 doesn't reap children, so a malformed
#   stop sequence could leave defunct processes lingering until the
#   container restarts.
# tzdata: makes TZ=America/New_York (set below) actually resolve so
#   date.today() in api.pipeline_phase2._derive_output_filename produces
#   filenames in US Eastern instead of container-default UTC — analysts
#   are based in EST so a 23:30 EST run would otherwise date as the
#   next day.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tini tzdata \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps first — single biggest layer, cache it on its own.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Copy app source.  The bundled NLTK corpora ship in the image so the
# walled-garden boot path still resolves without internet.
COPY api/         ./api/
COPY ml_package/  ./ml_package/
COPY phase3_package/ ./phase3_package/
COPY aic_utils.py ./
COPY nltk_data/   ./nltk_data/

# The static frontend lands where api/main.py mounts it (web/out at /).
COPY --from=web-build /web/out ./web/out

# Belt-and-braces: the bootstrap reads NLTK_DATA when set; setting it
# explicitly means a child process (e.g. a future CLI tool) inherits.
ENV NLTK_DATA=/app/nltk_data
ENV PYTHONUNBUFFERED=1
# US Eastern — see tzdata install above for context.
ENV TZ=America/New_York

EXPOSE 8000

# Healthcheck so Docker Desktop shows green when the API is reachable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# tini as PID 1 forwards SIGTERM to uvicorn (which runs the lifespan
# shutdown branch) and reaps any pipeline subprocess that exits between
# our lifespan signal and uvicorn's grace window expiring.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
