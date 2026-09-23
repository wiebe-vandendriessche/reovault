# syntax=docker/dockerfile:1
#
# Multi-stage build (see docs/architecture.md): a downloader stage fetches
# the pinned `reolink-cli` release and its per-asset `.sha256`, verifies it,
# and extracts the two binaries it ships (`reolink-cli`, `reolink-gateway`).
# The runtime stage is `python:3.14-slim` with `uv`-installed deps, running
# as uid 1000. The gateway daemon stays supervised in-process by
# `GatewaySupervisor` (reovault/providers/gateway.py); no supervisord, no
# second entrypoint.

ARG REOLINK_CLI_VERSION=0.19.0

FROM python:3.14-slim AS reolink-cli-download
ARG REOLINK_CLI_VERSION
ARG TARGETARCH
WORKDIR /dl
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in \
        amd64) rl_arch=x86_64 ;; \
        arm64) rl_arch=arm64 ;; \
        *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    asset="reolink-cli-${REOLINK_CLI_VERSION}-external-linux-${rl_arch}.tar.gz"; \
    base_url="https://github.com/reolink/reolink-cli/releases/download/v${REOLINK_CLI_VERSION}"; \
    curl -fsSLo "$asset" "$base_url/$asset"; \
    curl -fsSLo "$asset.sha256" "$base_url/$asset.sha256"; \
    sha256sum -c "$asset.sha256"; \
    tar -xzf "$asset" --exclude='._*'; \
    mkdir -p /out; \
    cp "reolink-cli-${REOLINK_CLI_VERSION}-external-linux-${rl_arch}/bin/reolink-cli" /out/; \
    cp "reolink-cli-${REOLINK_CLI_VERSION}-external-linux-${rl_arch}/bin/reolink-gateway" /out/; \
    chmod +x /out/reolink-cli /out/reolink-gateway; \
    /out/reolink-cli --version | grep -qF "$REOLINK_CLI_VERSION"

FROM python:3.14-slim AS runtime
ARG REOLINK_CLI_VERSION
ARG REOVAULT_VERSION=dev
ENV REOLINK_CLI_VERSION=${REOLINK_CLI_VERSION} \
    REOVAULT_VERSION=${REOVAULT_VERSION} \
    PYTHONUNBUFFERED=1 \
    HOME=/home/reovault \
    REOVAULT_CONFIG_FILE=/config/reovault.toml \
    REOVAULT_STORAGE__CONFIG_DIR=/config \
    REOVAULT_STORAGE__DB_PATH=/config/reovault.db \
    REOVAULT_STORAGE__MASTER_KEY_PATH=/config/master.key \
    REOVAULT_STORAGE__VAULT_DIR=/vault \
    REOVAULT_STORAGE__STAGING_DIR=/staging \
    REOVAULT_REOLINK_CLI__PINNED_VERSION=${REOLINK_CLI_VERSION} \
    REOVAULT_WEB__HOST=0.0.0.0

RUN groupadd -g 1000 reovault && useradd -u 1000 -g reovault -m -d /home/reovault reovault

COPY --from=reolink-cli-download /out/reolink-cli /out/reolink-gateway /usr/local/bin/

RUN pip install --no-cache-dir uv==0.12.*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY reovault ./reovault
COPY reovault.example.toml README.md ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

RUN mkdir -p /config /vault /staging /home/reovault/.config/reolink-cli \
    && chown -R reovault:reovault /app /config /vault /staging /home/reovault

USER reovault

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else sys.exit(1)"

ENTRYPOINT ["reovault"]
CMD ["daemon"]
