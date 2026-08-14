# syntax=docker/dockerfile:1.7

FROM node:24-bookworm-slim AS dsh-build

ARG DSH_VERSION=0.1.0-rc.6

ENV XDG_CACHE_HOME=/tmp/dsh-build-cache \
    NPM_CONFIG_CACHE=/tmp/npm-cache \
    PATH=/opt/dsh/node_modules/.bin:$PATH

WORKDIR /opt/dsh

COPY package.json package-lock.json ./

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      python3 \
    && rm -rf /var/lib/apt/lists/* \
    && npm ci --omit=dev --no-audit --no-fund \
    && test "$(dsh --version)" = "${DSH_VERSION}" \
    && npm cache clean --force

FROM node:24-bookworm-slim

ARG DSH_VERSION=0.1.0-rc.6

LABEL org.opencontainers.image.title="DeepSeek Harness All-in-One for Hugging Face Spaces" \
      org.opencontainers.image.source="https://github.com/BlueSkyXN/DSH-all-in-one-HFS" \
      org.opencontainers.image.licenses="GPL-3.0-only AND MIT" \
      io.huggingface.hfs.upstream.repository="https://github.com/deepseek-ai/deepseek-harness" \
      io.huggingface.hfs.upstream.package="@deepseek-ai/dsh@${DSH_VERSION}"

ENV NODE_ENV=production \
    NODE_OPTIONS=--enable-source-maps \
    DSH_VERSION=${DSH_VERSION} \
    DSH_HOME=/data/dsh \
    DSH_WORKSPACE=/data/dsh/workspace \
    DSH_INTERNAL_PORT=3080 \
    DSH_TRUSTED_HOST=blueskyxn-dsh-all-in-one-hfs.hf.space \
    HOME=/data/dsh/home \
    XDG_CACHE_HOME=/tmp/dsh-cache \
    NPM_CONFIG_CACHE=/tmp/npm-cache \
    PORT=7860 \
    PATH=/opt/dsh/node_modules/.bin:$PATH

WORKDIR /opt/dsh

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      apache2-utils \
      bash \
      ca-certificates \
      curl \
      git \
      nginx-light \
      python3 \
      ripgrep \
      tini \
      util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o node -g node -m 0700 \
      /data \
      /data/dsh \
      /data/dsh/home \
      /data/dsh/workspace \
      /tmp/dsh-cache \
      /tmp/npm-cache \
      /tmp/dsh-nginx

COPY --from=dsh-build /opt/dsh /opt/dsh
COPY space.cordis.yml /opt/dsh-hfs/space.cordis.yml
COPY nginx.conf /etc/nginx/nginx.conf
COPY entrypoint.sh /usr/local/bin/dsh-hfs-entrypoint

RUN chmod 0644 /opt/dsh-hfs/space.cordis.yml /etc/nginx/nginx.conf \
    && chmod 0755 /usr/local/bin/dsh-hfs-entrypoint \
    && test "$(dsh --version)" = "${DSH_VERSION}"

VOLUME ["/data"]
USER node
WORKDIR /data/dsh/workspace

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=5 \
    CMD ["curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:3080/"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/dsh-hfs-entrypoint"]
