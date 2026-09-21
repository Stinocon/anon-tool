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
| 2 | Docker packaging, one-command start | planned |
| 3 | public release: docs, license, CI, generic catalogs | planned |

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
python3 web/server.py                         # -> http://127.0.0.1:1407  (loopback only)
```

The UI has **no authentication** and is therefore bound to loopback only: it refuses a
non-loopback `--host` unless `--allow-lan` is passed, every request must carry the correct `Host`
and a per-run token, and no client-supplied filesystem path is ever used. See
`docs/DESIGN.md` §7 for the full perimeter.

The engine lives at `~/.anon/` and is mirrored into this repository by
`scripts/sync-from-live.sh` (code only — maps, `entities.txt` and `allow.txt` never leave the
machine).
