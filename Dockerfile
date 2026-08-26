FROM node:20-alpine AS web-build
WORKDIR /src/frontend
COPY frontend/package.json frontend/tsconfig.json frontend/tsconfig.app.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src
COPY frontend/public ./public
RUN npm install --no-audit --no-fund && npm run build

FROM tindy2013/subconverter:latest

ARG TARGETARCH=amd64
ARG MIHOMO_VERSION=1.19.30
RUN apk add --no-cache python3 py3-pip tzdata curl gzip && \
    python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    case "$TARGETARCH" in \
      amd64) MIHOMO_SHA=cf06ce2c7d1421bdbda14ee4a5b6046672dc35ebf8eecd8e77504ec3c0ed9a84 ;; \
      arm64) MIHOMO_SHA=58896873736d28628f66de3677c8654fa0f180662523148e136cff4f6e890069 ;; \
      *) echo "Unsupported TARGETARCH: $TARGETARCH"; exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/MetaCubeX/mihomo/releases/download/v${MIHOMO_VERSION}/mihomo-linux-${TARGETARCH}-v${MIHOMO_VERSION}.gz" -o /tmp/mihomo.gz && \
    echo "${MIHOMO_SHA}  /tmp/mihomo.gz" | sha256sum -c - && \
    gzip -dc /tmp/mihomo.gz > /usr/local/bin/mihomo && chmod 0755 /usr/local/bin/mihomo && \
    rm /tmp/mihomo.gz

ENV PATH="/opt/venv/bin:$PATH" \
    DATA_DIR=/data \
    SUBCONVERTER_URL=http://127.0.0.1:25500 \
    RULE_CONFIG=config/submanager.ini \
    PUBLIC_BASE_URL= \
    SITE_NAME="Sub Manager" \
    FORWARDED_ALLOW_IPS="127.0.0.1" \
    TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY --from=web-build /src/app/static /app/app/static
COPY replacements/ /base/
COPY supervisord.conf /etc/supervisord.conf
RUN mkdir -p /data/cache /data/upstreams /data/backups

VOLUME ["/data"]
EXPOSE 7777
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD curl -fsS http://127.0.0.1:7777/api/health || exit 1
ENTRYPOINT ["/opt/venv/bin/supervisord", "-c", "/etc/supervisord.conf"]
