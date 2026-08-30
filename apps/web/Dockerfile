FROM node:20-alpine

WORKDIR /app

COPY apps/web/package*.json ./apps/web/
RUN cd ./apps/web && npm ci \
    --registry=https://registry.npmmirror.com \
    --no-audit \
    --no-fund

COPY . .

WORKDIR /app/apps/web

# Next.js resolves rewrites while building the production bundle.
# Use the Compose service name here; a runtime-only API_URL is too late.
ARG API_URL=http://api:8000
ENV API_URL=${API_URL}
ARG CANGZHI_WEB_BASE_PATH=
ENV CANGZHI_WEB_BASE_PATH=${CANGZHI_WEB_BASE_PATH}

RUN npm run build

ENV NODE_ENV=production

CMD ["npm", "run", "start", "--", "--hostname", "0.0.0.0"]
