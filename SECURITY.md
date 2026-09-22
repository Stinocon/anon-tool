# Security policy

## Reporting

Open a private security advisory on this repository (or contact the maintainer directly). Please
do not open a public issue for a vulnerability.

## Threat model — what this tool is and is not

anon-tool is a **local** tool. Its whole security value is a boundary, and the boundary is:

- The engine (`anon.py`, `deanon.py`) is deterministic and stdlib-only: **no network capability**,
  no LLM, no telemetry. Anonymization that required a model would first have to hand the model the
  data it is supposed to protect.
- The web UI has **no authentication**. That is acceptable *only* because it binds `127.0.0.1`;
  the container publishes `127.0.0.1:1407:1407`, never `1407:1407`. Exposing the port to a network
  exposes your documents and your entity dictionary — that is a configuration decision, not a bug,
  and it is why the server refuses a non-loopback bind unless `--allow-lan` is passed explicitly.
- Requests must carry the correct `Host` header (an allowlist) and a per-run token delivered
  through a CSP nonce; a foreign `Origin` is refused when the header is present. This is what
  stands between a hostile page in your own browser and the API.
- **No client-supplied filesystem path is ever used**: uploads land in a per-request temp directory
  under generated names and are deleted afterwards. A redacted DOCUMENT is published in the private
  store (`~/.anon/downloads/`, mode 0600, pruned after an hour) and streamed from a token-named URL
  in 64 KB blocks: it is already redacted, so a leftover is not a leak, and it is never written next
  to the original. `/api/maps` exposes counts only, and the
  placeholder→real-value mapping is shown only behind an explicit, warned action with a timeout.
- size limits on uploads (160 MB, `ANON_MAX_UPLOAD_BYTES`) and on `/api/*` requests per minute (token bucket,
  `--rate-limit`, default 120/min, 0 disables), no shell, no `eval`, no content or value logging.
- the socket has a 30 s read timeout, so a client that announces a body and stalls cannot pin a
  worker thread; the external converter runs in its own process group, is killed as a group, is
  bounded in time (300 s, `ANON_CONVERT_TIMEOUT`) and in output — the cap is applied WHILE the
  output is produced (`ANON_CONVERT_MAX_BYTES`, default 160 MB), never after buffering it.
- the container runs as a non-root user, with a read-only root filesystem, `no-new-privileges`,
  and only the data volume (`/data`) plus a tmpfs writable.
- The one third-party component installed at build time (`firecrawl-anydoc`) is pinned by digest
  (`requirements-anydoc.txt`, `pip install --require-hashes`), so a replaced wheel fails the build
  instead of running.

## In scope

- any way to make the tool send document content, entity names or map values over the network;
- any way for a page from another origin to drive the API, read a response, or learn the token;
- any path that lets a request read or write outside the sandbox, or reach a subprocess argument
  in a dangerous position;
- any case where the tool reports success while leaving sensitive content in place, or restores
  values that do not belong to that document;
- any way the engine silently fails open without a visible indicator.

## Out of scope (declared limits, see `docs/DESIGN.md` §8)

- **contextual references** ("the client from Brescia") — redaction is deterministic pattern
  matching plus a curated dictionary; a human read of the redacted document is still required;
- **images/screenshots** — pixels are not scannable, so a screenshot of a client document passes;
- **`bash`-style reads** in the Pi guard are outside its default perimeter (`--anon-guard=all`
  extends it to shell output);
- **the guard's own false positives**: the guard scans with the same engine, so a heuristic that
  over-matches blocks a legitimate read (in this repository that happens on its own source and
  docs). The remedy is `~/.anon/allow.txt`, a session-only `--anon-guard-allow '/a/*,/b/*'` (or
  `/anon-allow <path>`), or `anon.py --allow-glob GLOB` for one run — never a workaround;
- **a check slower than the timeout or bigger than the cap** is refused, not read (fail-closed).
- **two runs cannot be confused**: the tag is part of every placeholder (`[EMAIL-1-a3f9d1]`), so a
  map from a different run leaves the tokens untouched and the restore fails loudly (exit 3)
  instead of substituting another client's values. The tag is 6 hex digits and is allocated
  against the tags **already in use** by the maps it can see (the default directory and the
  destination of `--map`): the width alone is not enough (`scripts/tag-collision.py` measures
  ~3% at 1 000 maps), so uniqueness is checked, not hoped for. Two runs allocating in the same
  instant are a declared residual race, and the check is per-directory: a map written under a
  different `ANON_HOME` is invisible to it. A mangled tag in the document makes restoration fail,
  by design.

## Supported versions

Only the current `main` (the latest tagged release) is supported.
