# syntax=docker/dockerfile:1
# Build context is apps/web/ ... but nginx config lives in infra/, so use the REPO ROOT as context:
#   docker build -f apps/web/Dockerfile .
FROM node:24-alpine AS build
WORKDIR /app/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
COPY docs/api/openapi.json /app/docs/api/openapi.json
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.31-alpine AS runtime
# Rendered to conf.d at start (envsubst); certificates are mounted at /etc/nginx/certs (runbook 1.4).
COPY infra/nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY infra/nginx/security-headers.conf /etc/nginx/pbip-security-headers.conf
COPY --from=build /app/apps/web/dist /usr/share/nginx/html
ENV PUBLIC_HTTPS_PORT=443
# 8080: HTTP -> HTTPS redirect and ACME challenges; 8443: TLS.
EXPOSE 8080 8443
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD wget -qO- http://127.0.0.1:8080/nginx-health >/dev/null || exit 1
