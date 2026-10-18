FROM node:20-alpine

WORKDIR /app

COPY apps/web/package*.json ./apps/web/
RUN cd ./apps/web && npm ci \
    --registry=https://registry.npmmirror.com \
    --no-audit \
    --no-fund

COPY . .

# Immutable image provenance; set by scripts/with-build-info.sh at build time.
ARG CANGZHI_VERSION=unversioned
ARG CANGZHI_REVISION=unknown
ARG CANGZHI_BUILD_TIME=unknown
ARG CANGZHI_BUILD_DIRTY=true
ENV CANGZHI_VERSION=${CANGZHI_VERSION} \
    CANGZHI_REVISION=${CANGZHI_REVISION} \
    CANGZHI_BUILD_TIME=${CANGZHI_BUILD_TIME} \
    CANGZHI_BUILD_DIRTY=${CANGZHI_BUILD_DIRTY}
LABEL org.opencontainers.image.version=${CANGZHI_VERSION} \
      org.opencontainers.image.revision=${CANGZHI_REVISION} \
      org.opencontainers.image.created=${CANGZHI_BUILD_TIME}

WORKDIR /app/apps/web

# Next.js resolves rewrites while building the production bundle.
# Use the Compose service name here; a runtime-only API_URL is too late.
ARG API_URL=http://api:8000
ENV API_URL=${API_URL}
ARG CANGZHI_WEB_BASE_PATH=
ENV CANGZHI_WEB_BASE_PATH=${CANGZHI_WEB_BASE_PATH}
ARG MAX_UPLOAD_SIZE_MB=50
ENV MAX_UPLOAD_SIZE_MB=${MAX_UPLOAD_SIZE_MB}

RUN npm run build

ENV NODE_ENV=production

CMD ["npm", "run", "start", "--", "--hostname", "0.0.0.0"]
