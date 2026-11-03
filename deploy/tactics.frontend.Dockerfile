FROM node:24.22.2-alpine3.23@sha256:9b69af715c7f9d038150b54ccccbaeffa9064bbc93e2904c29591ca097316af1 AS build
WORKDIR /opt/smokeline/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ .
# Keep these browser-only modules at the paths used by the existing formal UI.
COPY collaboration/src/stage-playback.mjs collaboration/src/stage-board-interactions.mjs collaboration/src/text-editing.mjs collaboration/src/text-history.mjs /opt/smokeline/collaboration/src/
ARG BUILD_MODE=test
RUN npm run build -- --mode "$BUILD_MODE"

FROM nginx:1.31.4-alpine@sha256:db35bfc6b2951e7f8a72db5db120288c127ffaeeb4a6d4b95a26fead017d5913
RUN addgroup -g 10002 -S smokeline \
    && adduser -u 10002 -S -D -H -G smokeline smokeline \
    && mkdir -p /tmp/client_temp /tmp/proxy_temp /tmp/fastcgi_temp /tmp/uwsgi_temp /tmp/scgi_temp \
    && chown -R 10002:10002 /tmp
COPY --from=build /opt/smokeline/frontend/dist /usr/share/nginx/html
USER 10002:10002
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
