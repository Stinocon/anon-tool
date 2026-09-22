# anon-tool — design notes

The reasoning behind the tool: what it is for, the decisions taken, and the limits that are
deliberate rather than accidental. If code and this file disagree, the code wins — then fix this
file.

## 1. The goal, stated precisely

Make a technically rich document (a vulnerability assessment, a report, a contract) **non
attributable**, while keeping it **useful**. Those two goals pull in opposite directions, and the
whole design lives in that tension:

- redact too little → the document still identifies the client;
- redact too much → the document stops being readable and stops being useful.

Consequence: not every "sensitive word" should be redacted by default. A city name alone rarely
identifies anyone and appears constantly ("the Brescia site"); redacting every Italian
municipality would shred a report. So low-signal catalogs are **opt-in and context-gated**,
while high-signal, machine-verifiable items are on by default.

## 2. Why the engine must not be an LLM

To anonymize text, a model must first receive that text. An LLM-based anonymizer therefore
transmits exactly what it claims to protect. That is not a tuning problem, it is a
contradiction, so the engine is:

- **deterministic** — regex + curated dictionaries + checksum validators; same input, same
  output, every time;
- **local** — stdlib only, no `socket`/`urllib`/`http` imports, no telemetry;
- **auditable** — a 700-line module with a 400-line test suite, not a model.

An LLM may be *attached later* as a **candidate suggester** on a local endpoint (see §8): it would
propose "this looks like a person's name", a human approves, and the deterministic engine applies
the substitution. The engine stays the only thing that ever writes.

## 3. Architecture

```
                  ┌──────────────────────────────────────────┐
   CLI / skill ──►│  engine: anon.py · deanon.py              │◄── web UI (server.py)
   Pi guard   ──► │  detect · anonymize · audit · deanon      │
                  └──────────────────────────────────────────┘
                        │                        │
                        ▼                        ▼
              <doc>.redacted.<ext>      ~/.anon/maps/<id>.map.json
                   (safe to share)      (REAL values, 0600, never in a repo)
```

One engine, several front-ends. **The web UI does not reimplement any detection logic** — it
imports the same module. Two copies of redaction logic diverge, and divergence in a privacy tool
is a silent hole. This is why the CLI, the Pi guard, the skill and the web app all inherit the
same tests and the same adversarial reviews.

## 4. Losslessness

`anon` replaces each sensitive span with a typed placeholder (`[EMAIL-1]`, `[AZIENDA-2]`) and
records `placeholder → {type, exact original}` in a map. Because the *exact substring* is stored,
`anon → deanon` reconstructs the document byte for byte, including spelling variants: `Contoso`,
`Contoso S.r.l.` and `CONTOSO` are each redacted and each restored exactly as written.

Placeholder-shaped text already present in the source is *reserved*, so a literal `[EMAIL-1]` in
the document is never mistaken for one this run produced.

## 4b. A document can only be restored with ITS map

Placeholder numbering is per document, so `[EMAIL-1]` of client A's run and `[EMAIL-1]` of client
B's run were indistinguishable: applying the wrong map silently substituted another client's real
values and reported success. Content alone can never tell two runs apart, so the link is carried
in the token itself:

```
[EMAIL-1-a3f9]      # 1st email of the run tagged a3f9
```

- `anon.py` generates a fresh tag per run (6 hex digits: 4 made a collision likely enough to be
  worth fixing), records it in the map, and stamps every
  placeholder with it. `--tag` pins it when reproducibility matters.
- `deanon.py` resolves placeholders exactly, so a foreign map leaves them untouched and the run
  reports INCOMPLETE (exit 3) instead of substituting the wrong values.
- The verdict is fail-closed: `complete` requires no unresolved placeholder, no unknown token,
  and at least one replacement. A document with nothing to restore is reported, never silently
  passed through.
- The old untagged form still parses, so maps created before this change keep working.

Cost: the model must copy `[EMAIL-1-a3f9]` verbatim. That is why the skill states it as a hard
rule, and why the failure is loud: a mangled token cannot be restored, and the run says so.

## 5. Dictionary matching (what "the same entity" means)

The dictionary is split by kind, all optional and merged: `~/.anon/entities.txt` (the generic
fallback), `~/.anon/people.txt` (`@type PERSONA`) and `~/.anon/clients.txt` (`@type AZIENDA`).
`--entities PATH` is repeatable and overrides the defaults. The files are operator data: they live
in `~/.anon`, never in a repository.

One entry covers a family of spellings, deterministically:

| Mechanism | Example |
|---|---|
| case folding | `Contoso` = `contoso` = `CONTOSO` |
| legal-form tolerance (optional suffix) | `Contoso` also matches `Contoso S.r.l.`, `contoso srl` |
| separators between words | `Acme Italia` matches `Acme-Italia`, `Acme.Italia` |
| Unicode normalization | an NFC entry matches an NFD (macOS) file |
| explicit aliases (`TYPE|value|alias`) | `Contoso` and `Contoso-Italia` |
| **stem** (`@stem`, planned) | `Pincopallino` also matches `Pincopallino1`, `Pincopallino-DB01` |

Deliberately *not* matched: intra-word variation (`Contoso-Italia` ≠ `Contoso` unless declared as an
alias). Fuzzy matching was rejected: in a privacy tool a fuzzy rule that silently misses is worse
than an explicit alias the operator adds once.

### 5b. Scanning cost (why the dictionary is not scanned entry by entry)

A 200-entry dictionary compiles to 400 patterns (one per normalization form). Scanning each one over
the whole text was the entire cost of `--check`: 13.7 s for 2 MB, measured, which is what made the
guard time out. The engine now locates candidates with ONE case-insensitive pass over the first
token of every entry and verifies each entry anchored at those positions — the same match, ~1.8-2.0
MB/s (`scripts/bench-check.py`). Entries with a `@context` cannot be found that way, so they are
grouped by context and the group pattern is used as a locator, again with anchored verification, so
`Roma` is still found next to `Roma Nord`.

Two detection rules carry their precision in a validator rather than in the pattern:

- **addresses** require a street marker, a name and a civic number, and at least one capitalized
  name token (read after an elided article: `d'Azeglio`, `dell'Università`). That is what separates
  `Via Roma 12` from `in via del tutto eccezionale, 3 volte`. Declared trade-off: an all-lowercase
  address (`via roma 12`) is not redacted.
- **`@stem`** entries warn (never refuse) below 5 characters: a stem matches any suffix, so a short
  one redacts unrelated words — and refusing would make the engine fail to load, which the Pi guard
  reads as "engine unreachable" and turns into a disabled guard.

## 6. Catalogs (built-in lists) and pattern groups

Built-in knowledge ships as **data**, in the same `TYPE|value` format as the custom dictionary, so
there is one parser and one mental model:

```
# catalogs/it-cities.txt
@type   CITTÀ
@match  case-sensitive
@context (?:comune|sede|stabilimento|filiale|magazzino|via|piazza|presso)\s+
Brescia
```

- `@match case-sensitive` + `@context` are what keep a low-signal catalog from shredding the text
  (`"the prato is green"` stays intact; `"sede di Brescia"` becomes `sede di [CITTÀ-1]`).
- Catalogs are **off by default** and ticked on demand ("anonimizza: …").
- Pattern groups (regex rules) are tagged (`identity`, `network`, `fintech`, `it-legal`) and
  toggled the same way.

High-signal patterns are on by default because their validators make them precise: **codice
fiscale** (16 chars + check character), **partita IVA** (11 digits + Luhn), **IBAN** (mod-97),
license plates, addresses. A checksum-validated match has almost no false positives — the
opposite of a word list.

## 7. Trust boundary of the local web app (implemented)

The web UI is the only part with a network surface, so its perimeter is explicit:

- **bind `127.0.0.1`** only; `0.0.0.0` is refused unless an explicit `--allow-lan` flag prints a
  warning. No authentication is acceptable *because* it is loopback — that is the boundary.
- **anti-CSRF / DNS-rebinding**: any page in the user's browser can `POST` to `127.0.0.1:1407`.
  Mitigations: `Host` allowlist, a per-run token required as a custom header (delivered to the page through a CSP nonce, so a foreign origin can never read it), and an `Origin` check when the header is present.
- **no client-supplied paths**: uploads land in a per-request temp directory under generated
  names and are deleted afterwards; the server never reads or writes an arbitrary path; results
  are streamed back as a download instead of written into the filesystem.
- size limits on uploads (160 MB) and on `/api/*` requests per minute (token bucket, `--rate-limit`),
  no shell, no `eval`, no content or value logging;
- the socket has a 30 s read timeout, so a client that announces a body and stalls cannot pin a
  worker thread; the external converter runs in its own process group, is killed as a group, is
  bounded in time (300 s, `ANON_CONVERT_TIMEOUT`) and in output — the cap is applied WHILE the
  output is produced (`ANON_CONVERT_MAX_BYTES`, default 160 MB), never after buffering it;
- the container runs as a non-root user, with a read-only root filesystem, `no-new-privileges`,
  and only the data volume (`/data`) plus a tmpfs writable.
- `/api/maps` exposes counts only; the placeholder→real-value mapping is shown only behind an
  explicit, warned action.

## 8. Planned seam for a local model

When a local model is available, it plugs in as a **detector**, never as the anonymizer:

```
engine.detect(text)  ->  [regex + dictionary + checksum matches]
engine.suggest(text) ->  [candidates from a detector backend]  -> human approves -> engine applies
```

A detector backend is a localhost endpoint (OpenAI-compatible or similar). Nothing in the engine
calls it implicitly; the engine itself keeps zero network capability.

## 9. Known limits (deliberate)

- contextual references ("the client from Brescia") — a human read is still required;
- proper names absent from the dictionary are not redacted;
- images/screenshots are not scannable (pixels), so a screenshot of a client document passes;
- office containers cannot be anonymized directly — they are converted to Markdown first, and
  `anon.py` refuses binary input rather than producing a corrupted file *named* "redacted";
- a file too large to scan is blocked (fail-closed), and so is a file whose check does not finish
  within the guard's timeout: the guard does not know whether it is sensitive, so it refuses rather
  than guesses (12 MB / 20 s, sized from `scripts/bench-check.py`);
- if the engine cannot run at all, the Pi guard fails open **with a visible indicator** — a
  broken checker must not brick the editor. That is the ONLY fail-open path left, and it is
  reserved for a genuinely missing engine (python absent, crash), never for a slow file.
