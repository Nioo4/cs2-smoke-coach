FROM node:24.22.2-alpine3.23@sha256:9b69af715c7f9d038150b54ccccbaeffa9064bbc93e2904c29591ca097316af1

ENV NODE_ENV=production
WORKDIR /opt/smokeline/collaboration
COPY collaboration/package.json collaboration/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts
COPY collaboration/src ./src
COPY collaboration/scripts/formal-container-server.mjs ./scripts/formal-container-server.mjs
USER node
EXPOSE 4186
CMD ["node", "scripts/formal-container-server.mjs"]
