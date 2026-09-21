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
| 0 | engine: office-aware `deanon`, residual detection, stem matching, checksum validators, catalogs, `audit` | in progress |
| 1 | local web UI (stdlib server + vanilla front-end, loopback only) | planned |
| 2 | Docker packaging, one-command start | planned |
| 3 | public release: docs, license, CI, generic catalogs | planned |

The engine lives at `~/.anon/` and is mirrored into this repository by
`scripts/sync-from-live.sh` (code only — maps, `entities.txt` and `allow.txt` never leave the
machine).
