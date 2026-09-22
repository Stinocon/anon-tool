<p align="center">
  <img src="docs/brand/banner.svg" alt="anon-tool — deterministic, local anonymization" width="860">
</p>

<p align="center">
  <em>Hand a document to an AI without handing over who it is about.</em>
</p>

<p align="center">
  <strong>v1.7.0</strong> — <a href="CHANGELOG.md">changelog</a>
</p>

---

## Why this exists

A vulnerability assessment that reads *"client X, three sites, the manager is Mr Y, 30 findings on
`X-PROD-01` at 1.2.3.4"* is, in practice, a list of names. Strip the names and the addresses, keep
the technical substance, and the document keeps all of its value while losing its identifiability.

`anon-tool` performs that substitution deterministically and locally: it replaces the identifying
parts of a document with typed placeholders (`[EMAIL-1-a3f9d1]`, `[CLIENTE-2-a3f9d1]`), leaves a
reversible map on your machine, and puts the real values back into the finished document at the
end — after the AI has worked on text that never named anybody.

## The rule the whole design follows

> The anonymizer is not an AI.

To anonymize a text, a model would first have to receive it: an LLM-based anonymizer transmits
exactly what it claims to protect. That is a contradiction, not a tuning problem. Detection is
therefore deterministic — regular expressions, a curated dictionary, and checksum validators for
Italian identifiers — and entirely local: standard library only, no network capability, no
telemetry, no credentials.

The AI re-enters only *after* redaction, when you or an agent analyze the redacted text.

## What it guarantees

- **No network capability.** The engine imports the Python standard library and nothing else; there
  is no `socket`, `urllib` or `http` import anywhere in it.
- **Lossless round trip.** `anon → deanon` restores the document byte for byte: the map stores the
  exact matched substring, so spelling variants come back as they were written.
- **Idempotent.** Anonymizing an already-redacted document changes nothing; placeholders present in
  the source are protected, not re-matched.
- **A document can only be restored with *its* map.** Every placeholder carries a per-map tag
  (`[EMAIL-1-a3f9d1]`). Applying a different run's map leaves the placeholders untouched and the
  command exits non-zero instead of substituting another client's values.
- **Open failure modes.** A document that still contains an unresolved placeholder, an unknown
  token, or nothing to restore at all is reported as *incomplete* — never passed through silently.
- **Validated identifiers.** Codice fiscale, partita IVA, IBAN and plate numbers are verified
  against their checksum, which keeps false positives near zero — the opposite of a word list.

## What it does not do

These are declared limits, not oversights:

- **Contextual references** ("the client from Brescia") are not detected. A redacted document still
  needs a human read before it leaves your hands.
- **Images and screenshots** cannot be scanned: pixels pass through, so do not paste a screenshot
  of a client document into a chat.
- **The custom dictionary is curated by hand.** A proper name that is not in the dictionary
  (`entities.txt`, `people.txt`, `clients.txt`) is not redacted. Keeping those files current is the
  one recurring maintenance task.
- **Shell reads** (`cat`, `rg`) are outside the Pi guard's default perimeter; `--anon-guard=all`
  extends it to shell output.
- **It is not a legal opinion** on using a cloud provider. It reduces technical risk; it does not
  replace a DPA or an internal assessment.

## Quick start

### Engine (no dependencies)

```bash
python3 anon.py report.txt                       # -> report.redacted.txt + a map in ~/.anon/maps/
python3 anon.py report.txt --check --json        # is this safe to read? (exit 1 if not)
python3 anon.py report.txt --dry-run             # what WOULD be redacted, and where — writes nothing
python3 anon.py ./cliente --batch --dry-run      # the same for a whole folder, before touching it
python3 anon.py ./cliente --batch                 # -> *.redacted.* next to each source (never over it)
python3 anon.py verbale.docx                     # -> verbale.redacted.docx: same type, same layout
python3 anon.py report.txt --audit               # is an ALREADY redacted file really redacted?
python3 anon.py verbale.redacted.docx --audit    # the same question, answered inside the package
python3 deanon.py final.docx <map.json>          # put the real values back (text or .docx/.xlsx/.odt)
python3 convert.py report.docx > report.md       # docx/pdf -> Markdown, locally
python3 anon.py --list-catalogs                  # which built-in lists are installed
python3 anon.py --prune-maps 90                  # list the maps older than 90 days (--yes deletes)
```

### Web UI

```bash
docker compose up -d                             # -> http://127.0.0.1:1407
docker compose --profile slim up -d anon-tool-slim   # text formats only, no converter, port 1408
# or, without Docker:
python3 web/server.py                            # --rate-limit N caps /api/* (default 120/min)
```

Four tabs, one primary action each: **Anonimizza**, **Deanonimizza**, **Verifica**, **Dizionario**.
Pattern groups and catalogs sit behind an *Opzioni* disclosure, so the default flow is: drop a
document, anonymize, read the result. Dark theme by default, with a light alternative.

![The Anonimizza tab: a document goes in, typed placeholders come out](docs/brand/ui-anonymize.png)

The capture is of a **synthetic** document (no real values): the placeholders are typed and carry
the tag of the map that produced them (`[AZIENDA-1-91810c]`), and that map is what restores the
real values — it stays in `~/.anon/maps/`, never in the browser.

Upload a document and you get **two artifacts from one redaction**: the document itself, redacted
in place (`verbale.redacted.docx` — same type, same layout, headers, footers and properties
rewritten where they live), and the Markdown the model reads, **derived from the already redacted
file**. They share one tag and one map, so they can never disagree; a PDF, or a package the engine
cannot open, falls back to the Markdown alone and says so. The reason not to redact the two
independently: the same placeholder would end up meaning two different values.

`make up | down | logs | native | test | smoke` wraps the same operations.

The upload cap is a default, not a wall: `ANON_MAX_UPLOAD_BYTES` (160 MB, exposed to the page
through `/api/state` so the UI can refuse an oversized file before spending the transfer) raises it
deliberately. Raising it does not raise the converter's own `ANON_CONVERT_TIMEOUT`, nor the 12 MB
the Pi guard is willing to read from a redacted Markdown.

`--batch` walks a directory and refuses what it cannot do safely: binary or unscannable files are
reported and skipped (never copied under a `redacted` name), files over 12 MB are skipped rather
than half-processed, its own `*.redacted.*` outputs are skipped so a second run cannot nest
placeholders, and a directory inside the private store is refused outright. `--batch --check` is the
folder-shaped gate (exit 1 if anything is sensitive); `--out DIR` keeps the sources' folder clean.
Office containers (`.docx`, `.xlsx`, `.pptx`, `.odt`) are rewritten in place, so `--batch` redacts
them too; legacy `.doc/.xls/.ppt`, PDFs and images are still skipped, and `--check` on a container
names the findings *and* keeps `unscannable` — the field the Pi guard's auto-remediation reads —
because that flag describes what the `read` tool would do with the file, not what this engine can.

The container mounts `~/.anon` at `/data`, so the UI, the CLI and the Pi guard all read the same
`entities.txt` and write to the same map directory: one source of truth.

## How the pieces fit

| Piece | Role |
|---|---|
| `anon.py` | engine: detection, redaction, map, `--check`, `--audit` |
| `deanon.py` | inverse: restores the real values, in plain text and inside `.docx/.xlsx/.pptx/.odt` |
| `convert.py` | document → Markdown, using a pinned [anydoc](https://github.com/firecrawl/anydoc) |
| `web/` | local UI: stdlib HTTP server plus a vanilla front-end (no framework, no CDN, no build) |
| `catalogs/` | built-in lists (Italian municipalities, consumer mail domains), opt-in |
| `~/.anon/maps/` | reversible maps — the only place the real values live, mode 0600, never in a repo |

The Pi integration (a skill and a guard extension) lives in
[pi-customization](https://github.com/Stinocon/pi-customization); the portable workbench it belongs
to is [pi-workbench](https://github.com/Stinocon/pi-workbench).

## The dictionary

The curated dictionary lives in `~/.anon`, split by kind so each file stays small — and it never
enters a repository:

- `entities.txt` — the generic fallback: anything that is not a person or a company (`TIPO|valore`).
- `people.txt` — people. Open it with `@type PERSONA` and then one value per line (`Mario Rossi`);
  an entry can carry an email as an alias (`Mario Rossi|m.rossi@x.it`).
- `clients.txt` — companies and their sites. Open it with `@type AZIENDA` and list the company name
  (and, if you want, an address, which is redacted like any other entry).

All three are optional and read together; `--entities PATH` (repeatable) overrides them. A line
without a `TIPO|` prefix takes the `@type` declared above it. Case (`Spa` = `SPA`) and legal forms
(`srl` = `S.r.l.`) are already folded, so one entry covers the whole family. The web UI's
**Dizionario** tab edits the three files through the same engine.

## The catalog system

A catalog is the same file format as the custom dictionary, with directives:

```
@type    CITTÀ
@match   case-sensitive               # `Brescia` matches, `il prato è verde` does not
@context (?:comune di|sede di)\s+     # only after a marker
@context off                          # stop requiring one for the entries that follow
@stem    on                           # `Pincopallino` also covers `Pincopallino1`, `-DB01`
Brescia
```

```bash
python3 anon.py file.txt --catalogs it-cities,free-mail-domains
python3 anon.py file.txt --patterns legal,identity
```

Every catalog is **off by default**. Redacting more is not automatically better: a report where
each city has become `[CITTÀ-1]` loses its substance and protects almost nothing extra. See
`catalogs/README.md`.

## Security posture

The web UI has **no authentication**, and that is acceptable *only* because it is bound to
loopback: the container publishes `127.0.0.1:1407:1407`, never `1407:1407`, and the server refuses
a non-loopback bind unless `--allow-lan` is passed explicitly. Requests must carry the correct
`Host` header and a per-run token delivered through a CSP nonce; a foreign `Origin` is refused. No
client-supplied filesystem path is ever used.

The API is additionally rate limited (token bucket, `--rate-limit`, 0 disables it), so a runaway
script cannot pin every worker thread, and the document converter's output is capped *while* it is
produced rather than after it has been buffered.

The Pi guard (and `anon.py --check`) treats a path listed in `~/.anon/allow.txt` as un-sensitive.
For a one-off or session-scoped exemption there is `anon.py --allow-glob GLOB` (repeatable) and, in
Pi, `--anon-guard-allow` / the `/anon-allow` command — so the permanent allowlist is reserved for
genuinely public paths. The allowlist is still evaluated by the engine, never by the guard.

A **binary document** read through Pi (docx, pdf, xlsx) is not a dead end: the guard converts and
anonymizes it locally (`--anon-guard-auto=ask|on|off`, default `ask`) and only the redacted
Markdown reaches the context. If the conversion fails, the read is blocked — the failure path is
the fail-closed block, never "clean". The guard's path is the *read*; for a document you intend to
**deliver**, `anon.py` (or the UI) hands back the same file type with the layout intact.

The full perimeter and the threat model are in [`SECURITY.md`](SECURITY.md) and
[`docs/DESIGN.md`](docs/DESIGN.md).

## Development

```bash
make test      # engine suite, web integration, UI load check, doc-number gate
make smoke     # build the container and exercise every endpoint
make up-slim   # the text-only container variant (port 1408)

# How fast can the engine check a file? The Pi guard's size cap is derived from this measurement,
# and the number depends on the dictionary size — run it with the dictionary you actually use.
python3 scripts/bench-check.py --mb 8 --entities 200
# Is 6 hex digits enough for the per-map tag? Measured, not argued.
python3 scripts/tag-collision.py
# What does .docx -> Markdown lose, and why the in-place path exists? (headers, metadata,
# comments, tracked changes — the six features the container pass now covers)
python3 scripts/convert-fidelity.py
# Refresh the converter's hashed pin after a version bump.
python3 scripts/pin-converter.py > requirements-anydoc.txt
```

The converter is installed from `requirements-anydoc.txt` with `--require-hashes`, so a swapped
wheel cannot enter the image or the native venv.

`docs/OPEN-ISSUES.md` records what is intentionally left for a later pass.

## License

MIT — see [`LICENSE`](LICENSE). Third-party components and their provenance are listed in
[`NOTICE.md`](NOTICE.md).
