# syntax=docker/dockerfile:1.7
#
# Multi-stage build on Docker Hardened Images (DHI).
#
# Why two stages?
#   1. Build stage installs pip + wheel deps using the ``-dev`` DHI
#      variant (has shell, pip, build tools, and apt for compiling any
#      C-extension wheels).
#   2. Runtime stage copies just the venv + app code. The default
#      runtime is also the ``-dev`` variant because the cloak backend
#      (default) launches Chromium via subprocess + needs a shell.
#      For lightpanda-only deploys, override ``BASE_RUNTIME`` with the
#      slim DHI Python (no shell, smaller attack surface) — see the
#      "lightpanda-only" section below.
#
# Hardened image registry: ``dhi.io/python:<tag>``. Tags follow
# Docker's DHI convention: ``<py-version>-dev`` (with shell), bare
# ``<py-version>`` (slim runtime), ``<py-version>-fips`` (FIPS-compliant
# OpenSSL), ``<py-version>-sfw-dev`` (Socket Firewall). Verify the
# exact tag available to your DHI subscription in the Docker Hub
# catalog before shipping; tags drift as upstream Python releases.
#
# Build variations
# ----------------
# Default (cloak-compatible):
#   docker build .
#
# Lightpanda-only (distroless runtime, smaller image):
#   docker build --build-arg BASE_RUNTIME=dhi.io/python:3.12 .
#
# With Docling (PDF / Word / PPT structured parse, ~1.5 GB models on
# first use):
#   docker build --build-arg INSTALL_DOCLING=1 .
#
# Non-DHI fallback (for local dev without DHI subscription):
#   docker build \
#     --build-arg BASE_BUILDER=python:3.12-slim \
#     --build-arg BASE_RUNTIME=python:3.12-slim .

ARG BASE_BUILDER=dhi.io/python:3.12-dev
ARG BASE_RUNTIME=dhi.io/python:3.12-dev

# ─── stage 1: builder ──────────────────────────────────────────────
# Install pip deps into an isolated venv. The dev variant has pip and
# build tooling; the slim runtime stage doesn't.
FROM ${BASE_BUILDER} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Docling is opt-in (~1.5 GB transformers+torch on first model
# download). To enable: ``docker build --build-arg INSTALL_DOCLING=1 .``
# Models cache under HF cache; mount a volume in prod or expect a
# one-time download on the first PDF / Word request.
#
# Lightpanda support is included by default (just the Playwright
# Python client; ~10 MB). The Lightpanda BINARY runs in a sibling
# container — see Dockerfile.lightpanda + docker-compose.yml.
ARG INSTALL_DOCLING=0

COPY requirements.txt pyproject.toml ./
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install -r requirements.txt && \
    if [ "${INSTALL_DOCLING}" = "1" ]; then \
        /opt/venv/bin/pip install "docling>=2.0"; \
    fi

# ─── stage 2: runtime ──────────────────────────────────────────────
FROM ${BASE_RUNTIME} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# The venv is portable as long as the Python version matches between
# builder and runtime. Use the same DHI Python major.minor across both
# args; the bare ``-dev`` and slim tags share the same interpreter.
COPY --from=builder /opt/venv /opt/venv

COPY main.py spider_server.py ./
COPY reasoners ./reasoners
COPY spider ./spider

EXPOSE 8005 11700

# Explicit STOPSIGNAL — docker defaults to SIGTERM but stating it lets
# operators (and future Dockerfile readers) know we rely on it. Python
# isn't PID 1 in some orchestrators; if you wrap with tini/dumb-init,
# they forward SIGTERM correctly.
STOPSIGNAL SIGTERM

# Exec form so the CMD works under both:
#   * dev runtime (has /bin/sh and could use shell form)
#   * slim runtime (distroless — no shell, exec form required)
CMD ["/opt/venv/bin/python", "main.py"]
