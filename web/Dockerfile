FROM node:24-alpine AS builder
ARG ROOMOS_RELEASE=development
ARG ROOMOS_REVISION=unknown
ENV VITE_ROOMOS_RELEASE=${ROOMOS_RELEASE} \
    VITE_ROOMOS_REVISION=${ROOMOS_REVISION} \
    VITE_ROOMOS_COMPONENT=web
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/tsconfig.json web/tsconfig.app.json web/tsconfig.node.json web/vite.config.ts web/index.html ./
COPY web/src ./src
COPY web/public ./public
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29-alpine
ARG ROOMOS_RELEASE=development
ARG ROOMOS_REVISION=unknown
ARG ROOMOS_BUILD_CREATED=1970-01-01T00:00:00Z
ARG ROOMOS_SOURCE_URI=urn:roomos:source
LABEL org.opencontainers.image.revision="${ROOMOS_REVISION}" \
      org.opencontainers.image.version="${ROOMOS_RELEASE}" \
      org.opencontainers.image.created="${ROOMOS_BUILD_CREATED}" \
      org.opencontainers.image.source="${ROOMOS_SOURCE_URI}" \
      org.opencontainers.image.title="RoomOS Web" \
      io.metehantech.roomos.component="web"
COPY --from=builder /build/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/roomos-common.inc /etc/nginx/includes/roomos-common.inc
USER 101:101
EXPOSE 8080 8443
