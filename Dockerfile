# syntax=docker/dockerfile:1

# ---- Tailwind standalone CLI (no Node), checksum-verified, builds the CSS ----
FROM python:3.13-slim-bookworm AS tailwind
ARG TARGETARCH
ARG TAILWIND_VERSION=4.3.5
ARG TAILWIND_SHA256_AMD64=e47695cee3f0d90623a91f3939b08bdee610eb5ad99340adff9d4145b284d6cc
ARG TAILWIND_SHA256_ARM64=ffcd0d21dbd7c605dc12743ff0c0b47debc963c86b399674084c1c8f2c96ecad
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) asset=tailwindcss-linux-x64; sha="$TAILWIND_SHA256_AMD64" ;; \
      arm64) asset=tailwindcss-linux-arm64; sha="$TAILWIND_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/${asset}"; \
    echo "${sha}  /usr/local/bin/tailwindcss" | sha256sum -c -; \
    chmod +x /usr/local/bin/tailwindcss
WORKDIR /src
COPY static/src static/src
COPY . .
RUN tailwindcss -i static/src/input.css -o static/css/app.css --minify

# ---- Runtime ----
FROM python:3.13-slim-bookworm AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /usr/local/bin/
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=0 \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev
RUN useradd --create-home --uid 1000 app
COPY --chown=app:app . .
COPY --from=tailwind --chown=app:app /src/static/css/app.css static/css/app.css
USER app
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "exec gunicorn config.wsgi --bind 0.0.0.0:${PORT:-8000}"]
