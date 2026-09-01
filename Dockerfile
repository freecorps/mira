# syntax=docker/dockerfile:1

# ── Stage 1: build the React UI ───────────────────────────────────
# Pinned to the *build* platform: the bundle is architecture-independent, so
# a cross-platform build (e.g. `--platform linux/arm64` from an amd64 host)
# runs node natively instead of emulating it — that emulation used to be the
# single slowest part of the arm64 image build.
FROM --platform=$BUILDPLATFORM node:20-slim AS ui-builder
WORKDIR /ui
COPY ui/mira/package.json ui/mira/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund
COPY ui/mira/ ./
RUN npm run build

# ── Stage 2: backend + bundled UI ─────────────────────────────────
FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/miracodeai/mira"
LABEL org.opencontainers.image.description="Self-hostable AI code reviewer"
LABEL org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app

# uv is mounted only for the install steps, so the runtime image does not
# carry it. UV_COMPILE_BYTECODE pre-compiles site-packages, which makes every
# `python -c` / `mira …` start faster; UV_LINK_MODE=copy is required because
# the cache mount lives on a different filesystem than /app.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:${PATH}"

# Layer 1 — third-party dependencies, resolved from the lockfile alone. It
# only changes when pyproject.toml/uv.lock change, so every source-only commit
# reuses it from the registry build cache instead of re-downloading ~100 MB
# of wheels (per architecture).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=from=ghcr.io/astral-sh/uv:0.12.8,source=/uv,target=/bin/uv \
    uv sync --locked --no-dev --no-install-project --extra serve --extra bedrock

# Layer 2 — the package itself. Small, and the one layer that changes every
# commit. Installed non-editable so /app/src is not needed at runtime.
COPY README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=from=ghcr.io/astral-sh/uv:0.12.8,source=/uv,target=/bin/uv \
    uv sync --locked --no-dev --no-editable --extra serve --extra bedrock

# Pull the built UI in from stage 1. webhooks.create_app() picks this up
# automatically and serves it at / with SPA fallback.
COPY --from=ui-builder /ui/dist /app/ui_dist

EXPOSE 8000
# ENTRYPOINT (not CMD) so `docker run … image --config /app/mira.yaml`
# appends the args to `mira serve` instead of replacing the command.
ENTRYPOINT ["mira", "serve"]
