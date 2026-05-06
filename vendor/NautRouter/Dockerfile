# NautRouter sidecar for NautGate. Vendored at known SHA in this repo.
FROM node:20-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY tsconfig.json ./
COPY src/ ./src/

ENV NAUT_PORT=8404 \
    NAUT_WS_PORT=8403

EXPOSE 8404

HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=5s \
    CMD curl -fsS http://localhost:8404/health || exit 1

CMD ["npm", "start"]
