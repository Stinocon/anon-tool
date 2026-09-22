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

An LLM may be *attached later* as a **candidate suggester** on a local endpoint (see §7): it would
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

The web app is the only front-end with a network surface, so its perimeter — bind address, token,
`Host`/`Origin`, no client paths, the caps, the container hardening — is stated where a reader
looking for a threat model goes: [`../SECURITY.md`](../SECURITY.md). This document stays on the
engine.

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
[EMAIL-1-a3f9d1]      # 1st email of the run tagged a3f9d1
```

- `anon.py` allocates a tag per run — 6 hex digits, checked against the tags the existing maps
  use. The check covers both the default maps directory and the destination of `--map`; it is not
  atomic, so two runs allocating in the same instant are a declared residual race. The width alone
  is not enough: ~3% at 1 000 maps, ~53% at 5 000 (`scripts/tag-collision.py` measures the curve),
  so uniqueness is checked, not argued from the birthday bound. `--tag` pins the tag verbatim,
  without the check — an explicit override.
- `deanon.py` resolves placeholders exactly, so a foreign map leaves them untouched and the run
  reports INCOMPLETE (exit 3) instead of substituting the wrong values.
- The verdict is fail-closed: `complete` requires no unresolved placeholder, no unknown token,
  and at least one replacement. A document with nothing to restore is reported, never silently
  passed through.
- The old untagged form still parses, so maps created before this change keep working.

Cost: the model must copy `[EMAIL-1-a3f9d1]` verbatim. That is why the skill states it as a hard
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
token of every entry and verifies each entry anchored at those positions — the same match. The
cost is now a function of the DICTIONARY, not only of the file: measured on a 2 MB corpus
(`scripts/bench-check.py`), 1.60 MB/s with 200 entries, 0.93 with 1 000, 0.20 with 7 900 (the
full-municipality size), and ~2.0 MB/s at 16 MB with a few hundred entries, where the per-run
overhead is amortised. The guard's 12 MB cap is sized for a few hundred entries: against the full
list it would need 60 s of a 20 s budget (OPEN-ISSUES 26). Entries with a `@context` cannot be
found that way, so they are
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
@context off
Prato
```

- `@match case-sensitive` + `@context` are what keep a low-signal catalog from shredding the text
  (`"the prato is green"` stays intact; `"sede di Brescia"` becomes `sede di [CITTÀ-1]`).
- **Ordering rule:** every directive applies to the entries that FOLLOW it and a later one replaces
  it, so `@context` is a property of a block, not of the file. `@context off` closes the block —
  otherwise narrowing a context would mean reordering the file.
- Catalogs are **off by default** and ticked on demand ("anonimizza: …").
- Pattern groups (regex rules) are tagged (`identity`, `network`, `fintech`, `it-legal`) and
  toggled the same way.

High-signal patterns are on by default because their validators make them precise: **codice
fiscale** (16 chars + check character), **partita IVA** (11 digits + Luhn), **IBAN** (mod-97),
license plates, addresses. A checksum-validated match has almost no false positives — the
opposite of a word list.

## 7. Planned seam for a local model

When a local model is available, it plugs in as a **detector**, never as the anonymizer:

```
engine.detect(text)  ->  [regex + dictionary + checksum matches]
engine.suggest(text) ->  [candidates from a detector backend]  -> human approves -> engine applies
```

A detector backend is a localhost endpoint (OpenAI-compatible or similar). Nothing in the engine
calls it implicitly; the engine itself keeps zero network capability.

## 8. Known limits (deliberate)

- contextual references ("the client from Brescia") — a human read is still required;
- proper names absent from the dictionary are not redacted;
- images/screenshots are not scannable (pixels), so a screenshot of a client document passes;
- office containers **are** anonymized directly: `.docx/.xlsx/.pptx/.odt` are ZIP packages, so the
  engine rewrites the text parts where they live (document, headers, footers, comments, notes,
  properties) and hands back the same file type, layout and styles intact. The alternative — convert
  to Markdown, redact that — is the right way to give a model a document but the wrong way to
  deliver one: measured (`scripts/convert-fidelity.py`, which proves each feature is in the fixture
  before looking for it in the Markdown — a guessed signature is how a measurement lies) 11 of 17
  features survive, and headers, footers, comments, tracked deletions and the core properties
  (title, **author**) are among those that do not. A client name in a letterhead was therefore
  neither redacted nor delivered, and stayed in the original `.docx` and in any PDF exported from
  it. The in-place path covers those six texts; the conversion remains the guard's path for a
  `read`, where the point is to put something in front of a model, not to produce a deliverable;
- **what the in-place path refuses**: legacy `.doc/.xls/.ppt`, PDF and images (formats we cannot
  rewrite while verifying the result) and any file that claims to be a container but is not a
  readable ZIP — refused with exit 2 and nothing written, rather than a corrupted file *named*
  "redacted";
- **the redaction of a container is verified on the OUTPUT, not on the intention.** The written file
  is re-read and every text part re-scanned with the same detectors; if a detected value survives,
  the output is deleted and the command fails. The verification must use the SAME view as the
  detection, which is not a detail: `visible_index` concatenates fragments (right for a
  self-delimiting token like `[EMAIL-1-tag]`) while `masked_index` puts one space where each tag was
  (right for a real value — which the entity patterns can then find even when Word split it across
  two runs, since they join tokens with `[\s...]+`). The first version verified with the
  concatenating view and reported "0 leftovers" while `dc:creator` was still intact: the check
  confirmed its own blind spot;
- **a value that only matches by joining two elements is REFUSED, not rewritten.** The detection
  view puts one space where a tag was, so a match can reach across a boundary: `</w:t></w:r><w:r><w:t>`
  is Word splitting a run, and that one is fine to cross; `</w:p><w:p>` or
  `</dc:title><dc:creator>` is not — the rewrite would put the placeholder in the first fragment and
  EMPTY the others, destroying text that belongs to another element, and the map would store the
  separator spaces as part of the value. Boundaries are classified (`RUN_LEVEL_TAGS`) and a match
  reaching across a structural one is refused with nothing written (found in adversarial review,
  with a two-paragraph fixture that used to lose its second paragraph);
- **bounds on what a container may expand to**: a part over 64 MB, a part expanding more than 200x
  its compressed size, or parts totalling over 256 MB are refused before decompression, and
  `--check` (the guard's path on a `read`) uses the 12 MB `SCAN_MAX_BYTES` budget instead. Without a
  cap, `zipfile` inflates a part into memory before anyone looks at it — and the per-character index
  costs one int and one 1-char str per character, tens of times the text: a 61 KB file can ask for
  gigabytes. Over the cap the answer is the fail-closed one;
- **a text part that cannot be decoded is refused, never passed through.** A NUL byte is not proof
  of binary: a UTF-16/32 part *without* a BOM was classified binary, never scanned, and — because
  the verification used the same classification — reported as "0 leftovers" while the value sat
  there. Wide encodings are now recognised by byte pattern (strict decode, so an undecodable XML
  part is refused instead of being rewritten lossily);
- **detection is per part, verification is joined, and the asymmetry is deliberate**: a value split
  ACROSS two parts is invisible to the per-part detectors but visible to the joined re-scan, which
  refuses the output. Stricter than the detection, never looser — the one direction that is safe;
- **metadata is REDACTED, never removed.** The placeholders go into `docProps/core.xml` like
  anywhere else, so the reverse direction restores the original exactly. Stripping metadata would be
  irreversible and is a separate, explicit choice;
- **one redaction, two artifacts.** The UI redacts the container once (one allocation, one map, one
  tag) and derives the Markdown from the already redacted file. Redacting the Markdown and the
  container independently with one tag would give two maps sharing it — `[EMAIL-1-tag]` meaning a
  different value in each artifact — and a restore holding the wrong map would resolve in silence;
- a file too large to scan is blocked (fail-closed), and so is a file whose check does not finish
  within the guard's timeout: the guard does not know whether it is sensitive, so it refuses rather
  than guesses (12 MB / 20 s, sized from `scripts/bench-check.py`);
- if the engine cannot run at all, the Pi guard fails open **with a visible indicator** — a
  broken checker must not brick the editor. That is the ONLY fail-open path left, and it is
  reserved for a genuinely missing engine (python absent, crash), never for a slow file.
