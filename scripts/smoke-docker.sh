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
# A free port by default: the smoke gate must not collide with a running instance on 1407.
PORT="${PORT:-$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')}"
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
echo "$REDACTED" | grep -qE '\[EMAIL-1-[0-9a-f]{6}\]' || fail "anonymize (tagged placeholder missing)"
echo "$REDACTED" | grep -q 'mario@contoso.it' && fail "anonymize left the value in place"
pass "/api/anonymize (tagged placeholder, value gone)"

# The container's own converter must be present, or docx/pdf silently degrade to "text only".
api "http://127.0.0.1:${PORT}/api/state" | grep -q '"converter": true' || fail "converter missing in the image"
pass "converter available inside the image"

STATUS=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/state")
[ "$STATUS" = "403" ] || fail "the API answered without a token (got $STATUS)"
pass "the API refuses requests without the token"

# End-to-end document path INSIDE the container: a real .docx must be converted by the image's
# own converter and come back redacted. Skipped (not failed) when pandoc is unavailable to build
# the fixture.
if command -v pandoc >/dev/null; then
  DOCX_DIR="$(mktemp -d "${TMPDIR:-/tmp}/anon-smoke-doc-XXXXXX")"
  printf 'Cliente Contoso, referente mario@contoso.it, server 10.42.7.19\n' > "$DOCX_DIR/doc.md"
  pandoc "$DOCX_DIR/doc.md" -o "$DOCX_DIR/doc.docx"
  DOC_RESULT=$(curl -fsS -X POST \
    -H "X-Anon-Token: ${TOKEN}" -H 'X-Filename: doc.docx' -H 'Content-Type: application/octet-stream' \
    --data-binary "@$DOCX_DIR/doc.docx" "http://127.0.0.1:${PORT}/api/anonymize-document")
  echo "$DOC_RESULT" | grep -q '"origin": "converted"' || fail "docx was not converted inside the container"
  echo "$DOC_RESULT" | grep -qE '\[AZIENDA-1-[0-9a-f]{6}\]' || fail "docx text was not redacted"
  echo "$DOC_RESULT" | grep -q 'contoso.it' && fail "docx conversion leaked the value"
  pass "docx converted + redacted inside the container"
  rm -rf "$DOCX_DIR"
else
  printf 'SKIP  docx end-to-end (pandoc not installed)\n'
fi

# The port must be published on loopback ONLY.
docker port anon-tool-smoke | grep -q "^1407/tcp -> 127.0.0.1:${PORT}$" || fail "port is not loopback-only"
pass "port published on 127.0.0.1 only"

printf '\nALL DOCKER SMOKE CHECKS PASSED\n'
