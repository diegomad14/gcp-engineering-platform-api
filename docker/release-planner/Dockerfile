FROM node:22.19.0-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/eng-platform
COPY docker/release-planner/package.json ./package.json
COPY docker/release-planner/package-lock.json ./package-lock.json
RUN npm ci --omit=dev --ignore-scripts --legacy-peer-deps \
    && npm cache clean --force
COPY docker/release-planner/release_planner.mjs docker/release-planner/release_planner.test.mjs ./
RUN npm test

ENTRYPOINT ["node", "/opt/eng-platform/release_planner.mjs"]
