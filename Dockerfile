# A container for the PolicyDesk API.
#
# Having a Dockerfile does NOT make this production-ready. See docs/DEPLOYMENT.md
# for the honest list of what is still missing (authentication, first of all).
# What a container does give you is a reproducible environment: the same Python,
# the same pinned packages, the same layout, on any machine.

# --- Stage 1: build the dependencies -----------------------------------------
#
# Two stages so the final image does not carry the build tools. It is smaller, and
# a smaller image has fewer packages that could need patching later.
FROM python:3.12-slim AS builder

# uv installs far faster than pip and resolves from our pinned versions.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Copy ONLY the dependency manifest first. Docker caches layers, so this layer is
# rebuilt only when pyproject.toml changes -- not every time a source file does.
# Copying everything first would reinstall every package on every code change.
COPY pyproject.toml ./
COPY src/ ./src/

ENV UV_LINK_MODE=copy
RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache .

# --- Stage 2: the image that actually runs ------------------------------------
FROM python:3.12-slim AS runtime

# Run as a non-root user. If the process is compromised, the attacker has the
# rights of this user and not of root -- which is the difference between reading
# one directory and owning the container.
RUN useradd --create-home --uid 10001 policydesk

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=prod \
    LOG_JSON=true

WORKDIR /app
COPY --chown=policydesk:policydesk src/ ./src/
COPY --chown=policydesk:policydesk pyproject.toml ./

# The data directory is a VOLUME, not part of the image. The index and the
# database are state: baking them in would mean rebuilding the image to add a
# document, and would put one company's expense data inside a shareable artefact.
RUN mkdir -p /app/data && chown -R policydesk:policydesk /app/data
VOLUME ["/app/data"]

USER policydesk
EXPOSE 8000

# The container's own health check. An orchestrator uses this to decide whether to
# send traffic. Note it calls /healthz (am I alive) rather than /readyz (can I
# serve): a readiness failure should stop traffic, not restart the process.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# 0.0.0.0 inside a container means "this container's network interface", not "the
# public internet" -- what the port is exposed to is decided by how you run it.
# There is still no authentication, so do not publish this port publicly.
CMD ["python", "-m", "policydesk.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]
