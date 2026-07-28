FROM node:20-alpine

WORKDIR /app

COPY apps/web/package*.json ./apps/web/
RUN cd ./apps/web && npm ci \
    --registry=https://registry.npmmirror.com \
    --no-audit \
    --no-fund

COPY . .

WORKDIR /app/apps/web

CMD ["npm", "run", "dev"]
