#!/usr/bin/env bash
#
# Syntax-check both nginx configurations inside the real nginx image.
#
# nginx resolves upstream hostnames while parsing, so `core` is mapped to
# loopback for the test. Nothing is started and no network is used.
#
# Run locally with:  bash .github/scripts/nginx-validate.sh

set -euo pipefail

IMAGE='nginxinc/nginx-unprivileged:1.29-alpine'

TLS_DIR=$(mktemp -d)
trap 'rm -rf "$TLS_DIR"' EXIT

# A throwaway self-signed pair so nginx can parse the ssl_certificate
# directives. It lives in a temporary directory outside the repository for the
# duration of this script and is deleted on exit.
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj '/CN=roomos.example.com' \
  -keyout "$TLS_DIR/privkey.pem" \
  -out "$TLS_DIR/fullchain.pem" >/dev/null 2>&1
# The container runs as an unprivileged user, so it must be able to traverse
# the directory and read the pair.
chmod 755 "$TLS_DIR"
chmod 644 "$TLS_DIR/privkey.pem" "$TLS_DIR/fullchain.pem"

check() {
  local conf=$1 label=$2
  echo "==> ${label}"
  docker run --rm --network none --add-host core:127.0.0.1 \
    -v "$PWD/docker:/roomos:ro" \
    -v "$TLS_DIR:/run/roomos-tls:ro" \
    --entrypoint sh "$IMAGE" -eu -c "
      mkdir -p /etc/nginx/includes
      cp /roomos/roomos-common.inc /etc/nginx/includes/roomos-common.inc
      cp /roomos/${conf} /etc/nginx/conf.d/default.conf
      nginx -t
    "
}

check nginx.conf 'default HTTP configuration'
check nginx-tls.conf 'TLS overlay configuration'

echo
echo 'nginx configurations are valid.'
