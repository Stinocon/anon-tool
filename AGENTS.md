# Working on anon-tool — agent instructions

Project-specific rules for this repository. They add to the global `~/.pi/agent/AGENTS.md` and the
invariants in `.pi/decisions/`; they do not replace them.

## The running container must serve the current code

The Docker image bakes the code — whatever `anon.code_fingerprint` covers; that constant is the
single source for the file set — while the
`~/.anon:/data` volume carries only data. A committed and pushed fix is therefore **invisible in the
browser until the container is rebuilt and restarted**, and the version number does not move between
commits, so a stale container looks exactly like a current one on the page.

Rules:

1. **After changing any file the image ships, end the task by rebuilding and restarting it:**
   `make up MODEL=1`. The `MODEL=1` keeps the suggestion-model sidecar attached — without it,
   recreating the UI orphans the model container, which shares the UI's network namespace.
2. **Then require `python3 scripts/check-container-fresh.py` to exit 0.** It fingerprints the shipped
   code (the files `anon.code_fingerprint` covers) in the repository and inside every running container and fails when they
   differ. The same command is the `container-fresh` gate in `.pi/verify.json`, the `verify_command`
   of `DEC-0024`, and `make check-container`.
3. **Do not declare a task finished while a running `anon-tool` container is stale.** If no container
   is running there is nothing to keep fresh, and the check passes — a machine that never runs Docker
   is not blocked.
4. **The version is not the build marker.** `/api/state` reports `build` (the fingerprint) and the
   header shows its first 8 characters; use that, not the version, to tell two builds apart.

## Declared limits

- Verification is a deterministic gate: `make test`, `make smoke`, and `check-container-fresh`. A
  green suite on a stale container is not a green result.
