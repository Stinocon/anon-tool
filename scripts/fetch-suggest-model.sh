#!/usr/bin/env bash
#
# fetch-suggest-model.sh — download the model the suggestion panel talks to, and VERIFY it.
#
# Qwen2.5-3B-Instruct, Q4_K_M: ~2 GB, instruction-following, competent Italian, and small enough
# for CPU inference. The task is extraction (name the strings worth redacting), not reasoning: a
# bigger model that thinks until its budget runs out answers nothing at all — measured with a 9B
# (~138 s and an empty reply on a rich text), which is why this one is bounded and small.
#
# The download is checked against the SIZE and SHA256 published by the model repository (read from
# its metadata API, and NOT yet confirmed against a real file — the constants below become verified
# the first time this script completes). The check happens BEFORE the file is put in place: a
# truncated or substituted download is refused, and an unverifiable one never looks installed.
set -euo pipefail

MODEL="${ANON_SUGGEST_MODEL_FILE:-qwen2.5-3b-instruct-q4_k_m.gguf}"
URL="${ANON_SUGGEST_MODEL_URL:-https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/${MODEL}}"
SHA256_EXPECTED="${ANON_SUGGEST_MODEL_SHA256:-626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d}"
SIZE_EXPECTED="${ANON_SUGGEST_MODEL_SIZE:-2104932768}"
DIR="${ANON_MODEL_DIR:-$HOME/.anon/models}"
DEST="$DIR/$MODEL"

mkdir -p "$DIR"
verify() {
  local file="$1" actual_size actual_sha
  actual_size=$(wc -c < "$file" | tr -d ' ')
  [ "$actual_size" = "$SIZE_EXPECTED" ] || { echo "size mismatch: $actual_size != $SIZE_EXPECTED" >&2; return 1; }
  actual_sha=$(shasum -a 256 "$file" | cut -d' ' -f1)
  [ "$actual_sha" = "$SHA256_EXPECTED" ] || { echo "sha256 mismatch: $actual_sha" >&2; return 1; }
  return 0
}

if [ -f "$DEST" ]; then
  if verify "$DEST"; then echo "model already present and verified: $DEST"; exit 0; fi
  echo "the file present does not verify; re-downloading" >&2
  mv "$DEST" "$DEST.invalid.$(date +%Y%m%d%H%M%S)"
fi

echo "downloading $(basename "$URL") (~2 GB) into $DIR"
if command -v curl >/dev/null 2>&1; then
  curl -L --fail --progress-bar -o "$DEST.part" "$URL"
else
  wget -O "$DEST.part" "$URL"
fi

if ! verify "$DEST.part"; then
  rm -f "$DEST.part"
  echo "REFUSING to install the file: it does not verify" >&2
  exit 1
fi
mv "$DEST.part" "$DEST"
echo "verified: $DEST"
