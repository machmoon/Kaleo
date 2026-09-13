# Review UI bundle. Built here so the runtime image needs no Node at all.
#
# Deliberately NOT pinned to --platform=$BUILDPLATFORM, even though the output is
# platform-independent JavaScript and pinning would spare this stage from QEMU on
# a multi-arch build: $BUILDPLATFORM exists only under BuildKit, and ci.yml's
# plain `docker build .` must keep working on a legacy builder, where the pin is
# a hard parse error. The emulation cost is paid in docker.yml instead.
FROM node:22-slim AS frontend

WORKDIR /frontend

# Manifest first, so a source edit does not invalidate the npm install layer.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# Cloud Run container. Slim base: the engine is pure Python plus OR-Tools.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

# Dependencies first, so a source edit does not invalidate the wheel layer.
COPY pyproject.toml README.md ./
COPY engine/ ./engine/
# ``cad`` is installed here on purpose, and it reverses docs/ai-cad-plan.md
# decision 1. That decision rejected shipping the OpenSCAD *renderer binary*:
# a 350-800 MB apt tree plus a CPU-spiking subprocess per preview, to produce
# a preview that is not the product. OCP is a different thing entirely -- a
# linked library the case path imports lazily (measured: 2.1 s first import,
# 215 MB installed) -- and since 2026-09-08 it *is* the product: it builds the
# geometry and computes the acceptance clauses. Without it the deployed
# service would refuse every case request in words. Different thing, different
# call.
RUN pip install --no-cache-dir -e ".[agents,cloud,adk,cad]"

COPY service/ ./service/
# The delivery routes (service/deliver.py) import this lazily and say
# "not installed" without it; copying it makes them work in the image.
COPY googleapps/ ./googleapps/

# service/app.py serves this at / -- same origin as /generate, so no CORS.
COPY --from=frontend /frontend/dist ./frontend/dist

# Unprivileged runtime user. Everything under /app is installed by root and only
# read at runtime, so the app needs no write access to its own tree.
RUN useradd --system --create-home --uid 10001 silkscreen
USER silkscreen

# Cloud Run sends SIGTERM and routes to $PORT.
EXPOSE 8080

# /healthz is answered before any other route and needs no API key, so it is a
# true liveness signal. urllib rather than curl: the slim base has no curl, and
# adding one would mean an apt layer for a one-line request.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/healthz', timeout=4).read()"]

CMD ["python", "-m", "service.app"]
