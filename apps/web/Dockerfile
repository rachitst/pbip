# syntax=docker/dockerfile:1
# Build context is apps/web/ ... but nginx config lives in infra/, so use the REPO ROOT as context:
#   docker build -f apps/web/Dockerfile .
FROM node:25-alpine AS build
WORKDIR /app/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
COPY docs/api/openapi.json /app/docs/api/openapi.json
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
COPY infra/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY infra/nginx/security-headers.conf /etc/nginx/pbip-security-headers.conf
COPY --from=build /app/apps/web/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD wget -qO- http://127.0.0.1:8080/ >/dev/null || exit 1
