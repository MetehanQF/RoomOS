FROM node:24-alpine AS builder
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/tsconfig.json web/tsconfig.app.json web/tsconfig.node.json web/vite.config.ts web/index.html ./
COPY web/src ./src
COPY web/public ./public
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29-alpine
COPY --from=builder /build/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY docker/roomos-common.inc /etc/nginx/includes/roomos-common.inc
USER 101:101
EXPOSE 8080 8443
