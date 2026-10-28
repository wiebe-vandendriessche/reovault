# syntax=docker/dockerfile:1
#
# Multi-stage build (see docs/architecture.md):
# - `dashboard` builds the Svelte SPA with Node; the image ships only the
#   static output, no Node runtime.
# - `tools` fetches the pinned reolink-cli, restic and rclone releases, checks
#   each against its published SHA-256, and runs it once.
# - `runtime` is `python:3.15-slim` with `uv`-installed deps, running as uid
#   1000: one process backs up the cameras and serves the dashboard.

ARG REOLINK_CLI_VERSION=0.20.0
ARG RESTIC_RELEASE=0.19.1
ARG RCLONE_RELEASE=1.75.2

# Static output is architecture-independent: build it once, natively.
FROM --platform=$BUILDPLATFORM node:26-alpine AS dashboard
WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
# --ignore-scripts: no dependency gets to run code at install time.
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

FROM python:3.15-slim AS tools
ARG REOLINK_CLI_VERSION
ARG RESTIC_RELEASE
ARG RCLONE_RELEASE
ARG TARGETARCH
WORKDIR /dl
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl bzip2 unzip \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in \
        amd64) rl_arch=x86_64 ;; \
        arm64) rl_arch=arm64 ;; \
        *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    mkdir -p /out; \
    # reolink-cli: per-asset .sha256
    asset="reolink-cli-${REOLINK_CLI_VERSION}-external-linux-${rl_arch}.tar.gz"; \
    base="https://github.com/reolink/reolink-cli/releases/download/v${REOLINK_CLI_VERSION}"; \
    curl -fsSLo "$asset" "$base/$asset"; \
    curl -fsSLo "$asset.sha256" "$base/$asset.sha256"; \
    sha256sum -c "$asset.sha256"; \
    tar -xzf "$asset" --exclude='._*'; \
    cp "reolink-cli-${REOLINK_CLI_VERSION}-external-linux-${rl_arch}/bin/reolink-cli" \
       "reolink-cli-${REOLINK_CLI_VERSION}-external-linux-${rl_arch}/bin/reolink-gateway" /out/; \
    # restic: SHA256SUMS
    asset="restic_${RESTIC_RELEASE}_linux_${TARGETARCH}.bz2"; \
    base="https://github.com/restic/restic/releases/download/v${RESTIC_RELEASE}"; \
    curl -fsSLo "$asset" "$base/$asset"; \
    curl -fsSLo restic.sums "$base/SHA256SUMS"; \
    grep " $asset\$" restic.sums | sha256sum -c -; \
    bunzip2 -c "$asset" > /out/restic; \
    # rclone: SHA256SUMS
    asset="rclone-v${RCLONE_RELEASE}-linux-${TARGETARCH}.zip"; \
    base="https://github.com/rclone/rclone/releases/download/v${RCLONE_RELEASE}"; \
    curl -fsSLo "$asset" "$base/$asset"; \
    curl -fsSLo rclone.sums "$base/SHA256SUMS"; \
    grep -a " $asset\$" rclone.sums | sha256sum -c -; \
    unzip -p "$asset" "rclone-v${RCLONE_RELEASE}-linux-${TARGETARCH}/rclone" > /out/rclone; \
    chmod +x /out/*; \
    for check in "reolink-cli --version:$REOLINK_CLI_VERSION" "restic version:$RESTIC_RELEASE" \
                 "rclone version:v$RCLONE_RELEASE"; do \
        out=$(/out/${check%%:*}); \
        case "$out" in *"${check#*:}"*) ;; *) echo "unexpected: $out" >&2; exit 1 ;; esac; \
    done

FROM python:3.15-slim AS runtime
ARG REOLINK_CLI_VERSION
ARG RESTIC_RELEASE
ARG RCLONE_RELEASE
ARG REOVAULT_VERSION=dev
ENV REOVAULT_VERSION=${REOVAULT_VERSION} \
    PYTHONUNBUFFERED=1 \
    HOME=/home/reovault \
    REOVAULT_CONFIG_FILE=/config/reovault.toml \
    REOVAULT_STORAGE__DATA_DIR=/data \
    REOVAULT_STORAGE__STAGING_DIR=/data/staging \
    REOVAULT_WEB__PASSWORD_FILE=/data/web_password \
    REOVAULT_WEB__SESSION_KEY_PATH=/data/web_session.key \
    REOVAULT_WEB__HOST=0.0.0.0 \
    REOVAULT_TOOLS__REOLINK_CLI_VERSION=${REOLINK_CLI_VERSION} \
    REOVAULT_TOOLS__RESTIC_VERSION=${RESTIC_RELEASE} \
    REOVAULT_TOOLS__RCLONE_VERSION=${RCLONE_RELEASE} \
    RESTIC_CACHE_DIR=/data/restic-cache \
    RESTIC_PASSWORD_FILE=/run/secrets/restic_password \
    RCLONE_CONFIG=/config/rclone/rclone.conf

RUN groupadd -g 1000 reovault && useradd -u 1000 -g reovault -m -d /home/reovault reovault

COPY --from=tools /out/ /usr/local/bin/

RUN pip install --no-cache-dir uv==0.12.*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY reovault ./reovault
COPY --from=dashboard /dashboard/build ./reovault/web/dist
COPY reovault.example.toml README.md ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

RUN mkdir -p /config/rclone /data /home/reovault/.config/reolink-cli \
    && chown -R reovault:reovault /app /config /data /home/reovault

USER reovault

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else sys.exit(1)"

ENTRYPOINT ["reovault"]
CMD ["daemon"]
