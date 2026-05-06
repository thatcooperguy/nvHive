# =============================================================================
# Council API — Multi-stage Dockerfile
# Target: NVIDIA Ubuntu Linux, rootless Docker / user namespaces
# Size target: <200MB final image
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: builder — install Python dependencies into a clean prefix
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# System build dependencies (removed after this stage)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only the files needed for dependency resolution first (layer-cache friendly)
COPY pyproject.toml ./
COPY README.md ./
# Provide a minimal package stub so pip can resolve the project metadata
# without needing the full source tree at this point.
RUN mkdir -p nvh && touch nvh/__init__.py

# Install project dependencies into an isolated prefix so we can COPY the
# whole tree into the production stage without dragging in build tools.
RUN pip install --upgrade pip --no-cache-dir \
    && pip install --no-cache-dir --prefix=/install ".[serve,nvidia]"

# Now copy the real source so the editable install is complete
COPY nvh/ nvh/

# Re-install in non-editable mode with the real source present
RUN pip install --no-cache-dir --prefix=/install ".[serve,nvidia]"


# -----------------------------------------------------------------------------
# Stage 2: production — lean runtime image
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS production

# Labels
LABEL org.opencontainers.image.title="nvHive API"
LABEL org.opencontainers.image.description="Rootless AI lab and multi-LLM routing API"
LABEL org.opencontainers.image.source="https://github.com/thatcooperguy/nvHive"

# Minimal runtime deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user (uid 1000) — matches typical Linux desktop uid,
# which means rootless Docker bind-mounts work without permission fights.
RUN groupadd --gid 1000 nvhive \
    && useradd --uid 1000 --gid nvhive --shell /bin/bash --create-home nvhive

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
WORKDIR /app
COPY --chown=nvhive:nvhive nvh/ nvh/
COPY --chown=nvhive:nvhive pyproject.toml ./

# Persistent data directory (SQLite DB, logs, etc.)
# The volume should be mounted here; we create it so the non-root user owns it.
RUN mkdir -p /data && chown nvhive:nvhive /data

# Environment
ENV NVH_HOME=/data \
    HIVE_CONFIG_HOME=/data/config \
    NVH_LOGS=/data/logs \
    NVH_STATE=/data/state \
    NVH_SUPPORT=/data/support \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:$PATH"

# Drop to non-root
USER nvhive

EXPOSE 8000

# Health check — poll the /v1/health endpoint every 30 s
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/v1/health || exit 1

CMD ["uvicorn", "nvh.api.server:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "30"]
