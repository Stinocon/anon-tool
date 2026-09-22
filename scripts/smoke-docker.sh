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
echo "$REDACTED" | grep -qE '\[EMAIL-1-[0-9a-f]{6,8}\]' || fail "anonymize (tagged placeholder missing)"
echo "$REDACTED" | grep -q 'mario@contoso.it' && fail "anonymize left the value in place"
pass "/api/anonymize (tagged placeholder, value gone)"

# The container's own converter must be present, or docx/pdf silently degrade to "text only".
api "http://127.0.0.1:${PORT}/api/state" | grep -q '"converter": true' || fail "converter missing in the image"
pass "converter available inside the image"

STATUS=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/state")
[ "$STATUS" = "403" ] || fail "the API answered without a token (got $STATUS)"
pass "the API refuses requests without the token"

# End-to-end document path INSIDE the container: a real .docx must come back as a REDACTED .docx
# (the same file type, rewritten part by part) and as the Markdown the model reads, both from ONE
# redaction. Skipped (not failed) when pandoc is unavailable to build the fixture.
if command -v pandoc >/dev/null; then
  DOCX_DIR="$(mktemp -d "${TMPDIR:-/tmp}/anon-smoke-doc-XXXXXX")"
  printf 'Cliente Contoso, referente mario@contoso.it, server 10.42.7.19\n' > "$DOCX_DIR/doc.md"
  pandoc "$DOCX_DIR/doc.md" -o "$DOCX_DIR/doc.docx"
  export DOC_RESULT
  DOC_RESULT=$(curl -fsS -X POST \
    -H "X-Anon-Token: ${TOKEN}" -H 'X-Filename: doc.docx' -H 'Content-Type: application/octet-stream' \
    --data-binary "@$DOCX_DIR/doc.docx" "http://127.0.0.1:${PORT}/api/anonymize-document")
  echo "$DOC_RESULT" | grep -q '"origin": "container"' || fail "docx was not rewritten in place"
  echo "$DOC_RESULT" | grep -q '"container_name": "doc.redacted.docx"' || fail "no redacted document offered"
  echo "$DOC_RESULT" | grep -qE '\[AZIENDA-1-[0-9a-f]{6,8}\]' || fail "docx text was not redacted"
  echo "$DOC_RESULT" | grep -q 'contoso.it' && fail "the response leaked the value"
  echo "$DOC_RESULT" | grep -q 'container_b64' && fail "the document must not travel inside the JSON"
  # Download it for real, then look INSIDE the parts: a response that merely looks right (a
  # docx-shaped name with the plaintext still inside) has to fail here.
  DOC_URL=$(echo "$DOC_RESULT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("container_url", ""))')
  [ -n "$DOC_URL" ] || fail "no download url for the redacted document"
  curl -fsS -H "X-Anon-Token: ${TOKEN}" "http://127.0.0.1:${PORT}${DOC_URL}" -o "$DOCX_DIR/out.docx" \
    || fail "the redacted document could not be downloaded"
  DOCX_DIR="$DOCX_DIR" python3 -c '
import io, json, os, sys, zipfile, pathlib
result = json.loads(os.environ["DOC_RESULT"])
tag = result["tag"]
path = pathlib.Path(os.environ["DOCX_DIR"]) / "out.docx"
assert zipfile.is_zipfile(path), "the downloaded document is not a container"
with zipfile.ZipFile(path) as archive:
    inside = "\n".join(archive.read(name).decode("utf-8", "replace") for name in archive.namelist())
assert "contoso.it" not in inside, "the value survived inside the document"
assert f"[EMAIL-1-{tag}]" in inside, "no placeholder inside the document"
assert f"[EMAIL-1-{tag}]" in result["redacted"], "the two artifacts do not share the tag"
' || fail "the downloaded document is not really redacted"
  pass "docx rewritten in place, verified inside the output"
  rm -rf "$DOCX_DIR"
else
  printf 'SKIP  docx end-to-end (pandoc not installed)\n'
fi

# The port must be published on loopback ONLY.
docker port anon-tool-smoke | grep -q "^1407/tcp -> 127.0.0.1:${PORT}$" || fail "port is not loopback-only"
pass "port published on 127.0.0.1 only"

# The text-only variant must be discoverable, and it must not exist only as an undocumented
# build arg. `config` is enough here: the image itself is built by the profile, not by this gate.
if docker compose version >/dev/null 2>&1; then
  SERVICES=$(cd "$REPO" && docker compose --profile slim config --services 2>/dev/null | sort | tr '\n' ' ')
  case "$SERVICES" in
    *anon-tool-slim*) pass "compose profile 'slim' exposes anon-tool-slim" ;;
    *) fail "compose profile 'slim' is missing (services: ${SERVICES:-none})" ;;
  esac
fi

printf '\nALL DOCKER SMOKE CHECKS PASSED\n'
