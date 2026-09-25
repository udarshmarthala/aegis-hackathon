# syntax=docker/dockerfile:1.7
# Aegis backend - one image serves both the API and the worker.
# Multi-stage so the runtime layer carries no build toolchain.

# ---------- builder ----------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential gcc \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src

# pip is a build-time tool. It is uninstalled from the venv once the install
# is done, and with it the whole of pip._vendor - a second, unmanaged
# dependency tree this project does not pick versions for and cannot patch.
# The image scan reports vulnerabilities in it against us, correctly:
# shipping it means shipping its CVEs. msgpack and setuptools both surfaced
# exactly this way. Nothing installs packages at run time, so nothing is
# lost, and a container that cannot install a package is also one an
# attacker cannot install a package into.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install . \
 && /opt/venv/bin/pip uninstall --yes pip setuptools wheel

# ---------- runtime ----------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is present for the container healthcheck only.
# The base image's own pip goes too, and for the same reason. The venv is
# built in the builder stage, so the runtime stage never needs an installer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall --yes pip \
 && groupadd --system --gid 10001 aegis \
 && useradd  --system --uid 10001 --gid aegis --create-home aegis

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=aegis:aegis migrations ./migrations
COPY --chown=aegis:aegis src ./src

# Never run as root: a container escape should not begin with uid 0.
USER aegis

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=40s --retries=5 \
  CMD curl -fsS http://localhost:8000/health/live || exit 1

# The worker service overrides this with its own entrypoint.
CMD ["uvicorn", "aegis.api.app:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-graceful-shutdown", "30"]
