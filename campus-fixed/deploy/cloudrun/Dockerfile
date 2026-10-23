# Build context: desktop/attention-desk. Digests verified against Docker Registry on 2026-10-08.
FROM node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS web
WORKDIR /build
RUN npm install --global pnpm@10.30.0
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY vite.config.js index.html campus.html ./
COPY src ./src
COPY public ./public
RUN pnpm exec vite build

FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime
LABEL org.opencontainers.image.title="Touxian Tianjin University"
LABEL org.opencontainers.image.source="https://github.com/sssssjw11/touxian"
LABEL org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CAMPUS_DATA_DIR=/var/lib/touxian HOME=/var/lib/touxian
WORKDIR /app
COPY requirements-campus.lock.txt requirements-cloud.lock.txt ./
RUN python -m pip install --no-cache-dir -r requirements-cloud.lock.txt \
    && useradd --uid 10001 --user-group --create-home campus \
    && install -d -o campus -g campus /var/lib/touxian
COPY server ./server
COPY config ./config
COPY campus-contracts ./campus-contracts
COPY deploy/cloudrun/probe_sources.py ./deploy/cloudrun/probe_sources.py
COPY deploy/campus/LICENSE /app/LICENSE
COPY --from=web /build/dist ./dist
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "server.campus.cloud_run", "serve"]
