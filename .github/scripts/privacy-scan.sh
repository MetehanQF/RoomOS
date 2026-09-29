#!/usr/bin/env bash
#
# Fail the build if a contribution carries deployment-specific or personal data.
#
# RoomOS is written against real houses, so this scans tracked files for the
# patterns that tend to leak: private and overlay addresses, home paths, key
# material, auth state and real credentials. It is a safety net, not a
# substitute for reviewing a diff.
#
# Documentation values are intentionally allowed: the RFC 5737 ranges
# 192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24, plus example.com,
# homeassistant.local and home.arpa.
#
# Run locally with:  bash .github/scripts/privacy-scan.sh

set -uo pipefail

if ! echo probe | grep -qP 'p.obe' 2>/dev/null; then
  echo "This script needs grep with PCRE support (grep -P)." >&2
  exit 1
fi

status=0

# Tracked files only. The lockfile and LICENSE are third-party text, and this
# script necessarily contains the patterns it looks for.
mapfile -d '' -t FILES < <(
  git ls-files -z \
    ':!:.github/scripts/privacy-scan.sh' \
    ':!:web/package-lock.json' \
    ':!:LICENSE'
)

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "No tracked files to scan." >&2
  exit 1
fi

# A line carrying the marker below is exempt. Use it only for values that are
# provably not real — test fixtures and documentation examples — and say why:
#
#   Settings(HA_TOKEN="not-a-real-token")  # privacy-scan:allow test fixture
ALLOW_MARKER='privacy-scan:allow'

check() {
  local label=$1 pattern=$2
  local hits
  # -I skips binary files, -n reports line numbers, -P enables PCRE.
  hits=$(grep -InP -- "$pattern" "${FILES[@]}" 2>/dev/null | grep -Fv "$ALLOW_MARKER")
  if [[ -n $hits ]]; then
    echo "::error::${label}"
    printf '%s\n' "$hits" | sed 's/^/    /'
    echo
    status=1
  fi
}

echo "Scanning ${#FILES[@]} tracked files..."
echo

# --- Private, CGNAT and overlay address space --------------------------------
# RFC 1918 plus 100.64.0.0/10, used by CGNAT and by overlay networks such as
# Tailscale.
check 'Private, CGNAT or overlay IP address. Use a hostname or an RFC 5737 range such as 192.0.2.10.' \
  '(?<![\d.])(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})(?![\d.])'

# --- Overlay / tailnet hostnames ---------------------------------------------
check 'Tailnet or overlay hostname. Use example.com or home.arpa.' \
  '[A-Za-z0-9-]+\.ts\.net\b|\b[A-Za-z0-9-]+\.tailscale\.com\b'

# --- MAC addresses -----------------------------------------------------------
check 'MAC address.' \
  '(?<![\dA-Fa-f:])(?:[\dA-Fa-f]{2}:){5}[\dA-Fa-f]{2}(?![\dA-Fa-f:])'

# --- Absolute home paths -----------------------------------------------------
# /home/roomos is the unprivileged in-container user from core/Dockerfile.
check 'Absolute home path. Use $HOME or /srv.' \
  '/(?:home|Users)/(?!roomos(?:[/"'"'"']|$))[A-Za-z0-9._-]+'

# --- Key material ------------------------------------------------------------
check 'Private key or certificate block.' \
  '-----BEGIN (?:[A-Z ]+ )?(?:PRIVATE KEY|CERTIFICATE)'

check 'SSH key material.' \
  'ssh-(?:rsa|ed25519|dss) +AAAA'

# --- Tokens and session material ---------------------------------------------
check 'Probable JWT or Home Assistant long-lived access token.' \
  '\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}'

check 'Bearer token with a literal value.' \
  'Bearer +[A-Za-z0-9._~+/-]{16,}'

check 'Cloud or service provider token.' \
  '(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{24,}|glpat-[A-Za-z0-9_-]{16,})'

check 'RoomOS session or CSRF cookie with a literal value.' \
  '(?:roomos_session|roomos_csrf)\s*[=:]\s*["'"'"']?[A-Za-z0-9._-]{16,}'

# --- Assigned secrets --------------------------------------------------------
# Catches HA_TOKEN=<value> while allowing the empty form used in .env.example.
# ${...} and $(...) references are configuration, not values.
check 'Secret assigned a literal value. Leave it empty in tracked files.' \
  '(?:HA_TOKEN|API_KEY|APIKEY|SECRET_KEY|PASSWORD|PASSWD)\s*[=:]\s*(?!["'"'"']?\s*[$<{])["'"'"']?[A-Za-z0-9._/+-]{8,}'

# --- Credentials in URLs -----------------------------------------------------
check 'Credentials embedded in a URL (RTSP, MQTT, HTTP).' \
  '(?:rtsp|rtmp|mqtts?|https?)://[^/\s:]+:[^/\s@]+@'

# --- Auth state --------------------------------------------------------------
if tracked_state=$(printf '%s\n' "${FILES[@]}" \
    | grep -E '(^|/)(data/|.*\.(db|sqlite3?|db-wal|db-shm)$|server-secret$)'); then
  echo "::error::Auth state committed. Device records, sessions and the server secret must never be tracked."
  printf '%s\n' "$tracked_state" | sed 's/^/    /'
  echo
  status=1
fi

echo
if [[ $status -ne 0 ]]; then
  cat >&2 <<'EOF'
Privacy scan failed.

Replace real values with placeholders:
  addresses   192.0.2.10, homeassistant.local
  hostnames   roomos.example.com, example.com, home.arpa
  entity IDs  light.living_room
  paths       $HOME/roomos, /srv/roomos

Secrets belong in .env and auth state in data/, neither of which is tracked.
See CONTRIBUTING.md.
EOF
  exit 1
fi

echo "Privacy scan passed."
