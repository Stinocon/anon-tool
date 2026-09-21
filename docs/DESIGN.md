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
`Contoso S.r.l.` and `Contoso` are each redacted and each restored exactly as written.

Placeholder-shaped text already present in the source is *reserved*, so a literal `[EMAIL-1]` in
the document is never mistaken for one this run produced.

## 4b. Residual risk: the map is not bound to the document (declared)

Placeholder numbering is **per document** (`[EMAIL-1]` is the first email of *that* run). Nothing
in the content itself can tell a document produced from run A from run B, so if the operator
picks client B's map for client A's document, the run will happily substitute B's real values and
report success. The tool cannot detect this, and pretending otherwise would be worse than saying
so.

What is done about it:

- `complete` is now **fail-closed**: it requires `remaining == 0` **and** `unknown == 0` **and**
  at least one replacement. A document containing `[EMAIL-9]`, or a document with no placeholder
  at all, is reported as INCOMPLETE (exit 3) instead of success.
- The report carries the map's provenance (`map_source`, `map_created`, `map_counts`), so an
  operator can see which run produced it — the only available signal.
- The web UI **preselects the map created by the current run** and shows date/source/counts in
  the picker.

The structural fix (a per-map tag inside the placeholder, e.g. `[EMAIL-1-a3f9]`) would make a
mismatch detectable, at the cost of a longer token the model has to carry through unchanged. It
is deliberately not done yet.

## 5. Dictionary matching (what "the same entity" means)

One entry covers a family of spellings, deterministically:

| Mechanism | Example |
|---|---|
| case folding | `Contoso` = `contoso` = `Contoso` |
| legal-form tolerance (optional suffix) | `Contoso` also matches `Contoso S.r.l.`, `Contoso srl` |
| separators between words | `Acme Italia` matches `Acme-Italia`, `Acme.Italia` |
| Unicode normalization | an NFC entry matches an NFD (macOS) file |
| explicit aliases (`TYPE|value|alias`) | `Contoso` and `Contoso` |
| **stem** (`@stem`, planned) | `Pincopallino` also matches `Pincopallino1`, `Pincopallino-DB01` |

Deliberately *not* matched: intra-word variation (`Contoso` ≠ `Contoso` unless declared as an
alias). Fuzzy matching was rejected: in a privacy tool a fuzzy rule that silently misses is worse
than an explicit alias the operator adds once.

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

## 7. Trust boundary of the local web app (planned)

The web UI is the only part with a network surface, so its perimeter is explicit:

- **bind `127.0.0.1`** only; `0.0.0.0` is refused unless an explicit `--allow-lan` flag prints a
  warning. No authentication is acceptable *because* it is loopback — that is the boundary.
- **anti-CSRF / DNS-rebinding**: any page in the user's browser can `POST` to `127.0.0.1:1407`.
  Mitigations: `Origin` check + a required custom header + a per-run token in the URL.
- **no client-supplied paths**: uploads land in a per-request temp directory under generated
  names and are deleted afterwards; the server never reads or writes an arbitrary path; results
  are streamed back as a download instead of written into the filesystem.
- size limits on uploads, no shell, no `eval`, no content or value logging;
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
- a file too large to scan is blocked (fail-closed), not silently skipped;
- if the engine cannot run at all, the Pi guard fails open **with a visible indicator** — a
  broken checker must not brick the editor.
