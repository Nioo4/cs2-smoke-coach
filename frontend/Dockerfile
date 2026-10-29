FROM node:24.15.0-alpine3.22 AS build

WORKDIR /opt/smokeline/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ .
ARG BUILD_MODE=production
RUN npm run build -- --mode "$BUILD_MODE"

FROM mcr.microsoft.com/playwright:v1.62.1-noble AS e2e

WORKDIR /opt/smokeline/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ ./

FROM nginx:1.31.4-alpine@sha256:db35bfc6b2951e7f8a72db5db120288c127ffaeeb4a6d4b95a26fead017d5913

RUN apk add --no-cache gettext-envsubst \
    && addgroup -g 10002 -S smokeline \
    && adduser -u 10002 -S -D -H -G smokeline smokeline \
    && mkdir -p /tmp/client_temp /tmp/proxy_temp /tmp/fastcgi_temp /tmp/uwsgi_temp /tmp/scgi_temp \
    && chown -R 10002:10002 /tmp

COPY --from=build /opt/smokeline/frontend/dist /usr/share/nginx/html

USER 10002:10002
EXPOSE 8080 8443
