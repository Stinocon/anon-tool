# anon-tool

Deterministic, **local** anonymization for documents you want to hand to an AI without handing
over *who they are about*.

A vulnerability assessment that says "client X, 3 sites, the manager is Mr Y, 30 findings on
`X-PROD-01` at 1.2.3.4" is a list of names. Strip the names and the addresses, keep the technical
substance, and the document keeps its value while losing its identifiability. That is the whole
point of this tool.

## The non-negotiable design rule

**The anonymizer is not an AI.** An LLM cannot anonymize anything without first *receiving* the
data it is supposed to protect. Detection is therefore deterministic (regex + curated
dictionaries + checksum validators) and entirely local: no network calls, no telemetry, no
credentials, stdlib only.

The AI re-enters only *after* redaction, when you (or an agent) analyze the redacted text.

## What it guarantees, and what it does not

Guaranteed by construction:

- the engine has no network capability (stdlib imports only, no `socket`/`urllib`/`http`);
- `anon → deanon` is byte-for-byte lossless: the reversible map stores the exact matched
  substring, so the final document gets the real values back;
- the map (which holds the real values) lives in a private directory, mode 0600, never inside a
  working repository.

Not guaranteed (declared limits, see `docs/DESIGN.md`):

- **contextual references** ("the client from Brescia") are not detected — a redacted document
  still needs a human read;
- the custom dictionary is curated by hand: a proper name that is not in it is not redacted;
- images/screenshots are not scannable (pixels), and `bash`-style reads are outside the guard's
  perimeter.

## Status

Work in progress, built in public-quality steps but kept **private** for now.

| Phase | Content | State |
|---|---|---|
| 0 | engine: office-aware `deanon`, residual detection, stem matching, checksum validators, catalogs, `audit` | done |
| 1 | local web UI (stdlib server + vanilla front-end, loopback only) | done |
| 2 | Docker packaging, one-command start | done |
| 3 | docs, `NOTICE`, CI, security policy | done (repository stays **private** for now) |

## Quick start (engine)

```bash
python3 anon.py report.txt                    # -> report.redacted.txt + a map in ~/.anon/maps/
python3 anon.py report.txt --check --json     # is it safe to read? (the Pi guard uses this)
python3 anon.py report.txt --audit            # is an ALREADY redacted file really redacted?
python3 deanon.py final.docx <map.json>       # put the real values back (text or .docx/.xlsx/.odt)
python3 anon.py --list-catalogs               # what built-in lists are installed
```

## Quick start (web UI)

```bash
docker compose up -d                          # -> http://127.0.0.1:1407
# or, without Docker:
python3 web/server.py
```

`make up | down | logs | native | test | smoke` for the same thing in one word. The container
mounts `~/.anon` at `/data`, so the UI, the CLI and the Pi guard all share the same
`entities.txt` and the same maps.

The UI has **no authentication**, so it is bound to loopback only — the container publishes
`127.0.0.1:1407:1407`, never `1407:1407`. Every request must carry the correct `Host` and a
per-run token (delivered to the page through a CSP nonce), a foreign `Origin` is refused, and no
client-supplied filesystem path is ever used. See `docs/DESIGN.md` §7 for the full perimeter and
`scripts/smoke-docker.sh` for the gate that proves it end to end.

## The web UI

Four tabs, one primary action each — the secondary controls (pattern groups, catalogs) live behind
an **Opzioni** disclosure so the default flow is: drop a document, anonymize, read the result.

| Tab | What it does |
|---|---|
| **Anonimizza** | document or pasted text → redacted text + a map; counts per type; download or copy |
| **Deanonimizza** | pick the map this run produced (it is preselected) and get the document with the real values back |
| **Verifica** | `--audit`: residual findings and dictionary variants, with the values masked unless you reveal them |
| **Dizionario** | read, edit, save and download `entities.txt` (validated before it is written) |

The UI is deliberately plain: no framework, no CDN, no build step, CSS tokens with light and dark
from the system preference.

The engine lives at `~/.anon/` and is mirrored into this repository by
`scripts/sync-from-live.sh` (code only — maps, `entities.txt` and `allow.txt` never leave the
machine).
