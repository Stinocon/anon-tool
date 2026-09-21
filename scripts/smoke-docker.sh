#!/usr/bin/env bash
#
# smoke-docker.sh — build the image and exercise the UI inside the container, end to end.
#
# This is the gate that "it starts and works" — a container that builds but cannot serve is a
# failure nobody notices until they need it. It uses a THROWAWAY data dir, never the real one.
#
#   bash scripts/smoke-docker.sh
#
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-anon-tool:smoke}"
PORT="${PORT:-1407}"
DATA="$(mktemp -d "${TMPDIR:-/tmp}/anon-smoke-XXXXXX")"
mkdir -p "$DATA/maps" "$DATA/catalogs"
printf 'AZIENDA|Contoso\n' > "$DATA/entities.txt"

cleanup() {
  docker rm -f anon-tool-smoke >/dev/null 2>&1 || true
  rm -rf "$DATA"
}
trap cleanup EXIT

fail() { printf 'FAIL  %s\n' "$*" >&2; exit 1; }
pass() { printf 'PASS  %s\n' "$*"; }

command -v docker >/dev/null || { echo "smoke: docker not available"; exit 2; }

docker build -q -t "$IMAGE" "$REPO" >/dev/null || fail "docker build"
pass "image builds"

docker run -d --name anon-tool-smoke \
  -p "127.0.0.1:${PORT}:1407" \
  -v "$DATA:/data" \
  --read-only --tmpfs /tmp \
  --security-opt no-new-privileges:true \
  "$IMAGE" >/dev/null || fail "docker run"

# The server is ready when it answers HTTP; the engine import happens at startup.
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
curl -fsS "http://127.0.0.1:${PORT}/" >/dev/null || fail "container serves no page"
pass "container serves the UI"

TOKEN=$(curl -fsS "http://127.0.0.1:${PORT}/" | grep -o 'ANON_TOKEN = "[^"]*"' | cut -d'"' -f2)
[ -n "$TOKEN" ] || fail "no token in the page"

api() { curl -fsS -H "X-Anon-Token: ${TOKEN}" -H 'Content-Type: application/json' "$@"; }

api "http://127.0.0.1:${PORT}/api/state" | grep -q '"schema": "anon/1"' || fail "api/state"
pass "/api/state"

REDACTED=$(api -X POST -d '{"text":"Cliente Contoso e mario@contoso.it\n"}' \
  "http://127.0.0.1:${PORT}/api/anonymize")
echo "$REDACTED" | grep -qE '\[EMAIL-1-[0-9a-f]{4}\]' || fail "anonymize (tagged placeholder missing)"
echo "$REDACTED" | grep -q 'mario@contoso.it' && fail "anonymize left the value in place"
pass "/api/anonymize (tagged placeholder, value gone)"

# The container's own converter must be present, or docx/pdf silently degrade to "text only".
api "http://127.0.0.1:${PORT}/api/state" | grep -q '"converter": true' || fail "converter missing in the image"
pass "converter available inside the image"

STATUS=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/state")
[ "$STATUS" = "403" ] || fail "the API answered without a token (got $STATUS)"
pass "the API refuses requests without the token"

# The port must be published on loopback ONLY.
docker port anon-tool-smoke | grep -q "^1407/tcp -> 127.0.0.1:${PORT}$" || fail "port is not loopback-only"
pass "port published on 127.0.0.1 only"

printf '\nALL DOCKER SMOKE CHECKS PASSED\n'
