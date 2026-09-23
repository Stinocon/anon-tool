# Open issues — for the next pass

State at 2026-09-23 (last reviewed): everything closed so far carries its evidence next to it. **One
engineering item is open**: #40, which is the operator's step rather than the code's — run the
encrypted backup and test a restore. #13 is withdrawn and #19 needs no action, both on purpose.
#26, the scan locator, closed on 2026-09-23 (7× on the dictionary that made it visible, no
regression on the guard's path).

The rule I applied to myself: a fix counts as closed only with a number, a test name, or a command
that produces the claim — not with "looks right".

## Closed in this pass (11/11)

| # | What it was | How it is closed | Evidence |
|---|---|---|---|
| 1 | The Pi guard scanned at most 2 MB while the UI accepts 160 MB, so a large redacted Markdown was blocked when the agent tried to read it. | The engine's entity scan was rewritten (one literal pass + anchored verification per entry instead of one full-text pass per entry), the cap and the timeout are now derived from a measurement, and a timeout **blocks** the file instead of switching the guard off (it used to fail OPEN: one slow file and every later read went unchecked). | `scripts/bench-check.py`: 0.15 MB/s -> **1.8-2.0 MB/s** (16 MB in 8.1 s, 200-entry dictionary). Cap 2 MB -> **12 MB**, timeout 15 s -> 20 s (~3x margin). Verified end-to-end in a fresh Pi process: a 14.4 MB file is blocked with the new message, a clean file is still readable. |
| 2 | `@stem` could over-match on short stems, redacting unrelated words. | The dictionary loader warns (once per stem, on stderr) when a stem body is shorter than 5 characters. It deliberately does **not** refuse: a dictionary that fails to load makes the engine unreachable, which the guard reads as "engine broken" and turns into a disabled guard. | `tests/test_anon.py::CliTest::test_short_stem_warns_and_a_long_one_does_not` |
| 3 | Address detection was noisy and blind at the edges: prose was redacted and common address forms were missed. Under the hood, three abbreviated markers were escaped twice in the pattern (so they matched a literal backslash and never fired), a dotted initial in a street name was rejected, and the civic suffix was cut off. | The rule was rebuilt from one shared source, with a validator that requires a capitalized name token — the signal that separates a real address from prose (`in via del tutto eccezionale, 3 volte` used to be redacted). | `tests/test_anon.py::AddressCorpusTest` (12 positive, 6 negative). Before: 6 misses and 3 false positives on that corpus. After: 0 and 0, with an all-lowercase address declared as a deliberate miss. |
| 4 | A placeholder split across two Word runs was detected but not repaired (`deanon` exited 3 and asked for a regenerated document). | Repaired by **distribution**: the real value goes in the first fragment, the other fragments are emptied. No markup is added, removed or merged, so formatting does not move and the part stays well-formed. A split across two different parts is still reported and still exits 3. | `tests/test_anon.py::DeanonContainerTest::test_split_placeholder_is_repaired_without_touching_the_markup` (asserts the paragraph count is unchanged and the XML still parses) + `::test_fragment_across_two_parts_is_still_reported` |
| 5 | The audit's near-miss search is bounded and truncates silently; `candidates_capped` was returned by the API but never shown. | The UI states it explicitly, in the audit panel, before the candidate list: "elenco parziale: la ricerca … si è fermata ai limiti (400 parole / 200 entità)". | `tests/test_web.py::StaticUiTest::test_a_capped_candidate_scan_is_stated_out_loud` |
| 6 | The reveal view kept the real values in the DOM with no timeout and no way to put them away. | A "Nascondi i valori" button plus an automatic relock after 60 s (`REVEAL_TTL_MS`); the relock empties `#mapping` and hides the button. | `tests/test_web.py::StaticUiTest::test_reveal_view_can_be_relocked` |
| 7 | No rate limiting on the local API: a runaway script could saturate it. | Token bucket on `/api/*`, applied **after** authentication (an unauthenticated flood is already refused cheaply by the token check), `--rate-limit PER_MINUTE` (default 120, 0 disables), `429` + `Retry-After` computed from the bucket. | `tests/test_web.py::RateLimitTest` (burst first, then sticky refusals, `Retry-After` > 1 with `--rate-limit 3`). |
| 8 | The converter capped its output **after** buffering it, so a zip bomb inflated memory before the check. | stdout is read in blocks and the child's **process group** is killed the moment the cap is passed (the converter spawns the real engine as a grandchild that inherits stdout, so killing only the direct child would leave the reader blocked); stderr is drained by a thread; the child is always reaped. `ANON_CONVERT_MAX_BYTES` / `ANON_CONVERT_TIMEOUT` make both bounds configurable. | `tests/test_web.py::ConverterCapTest::test_a_runaway_converter_is_cut_off` (a converter writing 80 MB against a 1 KB cap must answer quickly, and does). |
| 9 | The Docker build pinned anydoc by version, not by digest. | `requirements-anydoc.txt` pins `firecrawl-anydoc==0.2.3` to the published sha256 of every artifact, and both the image and `convert.py --install` install with `--require-hashes`. `scripts/pin-converter.py` regenerates the file and refuses to emit one whose dependency closure is not fully pinned. | `python3 -m pip install --dry-run --require-hashes -r requirements-anydoc.txt` in a fresh venv; `make smoke` builds the image through the same file. Closure measured: 1 package, 0 dependencies. |
| 10 | `~/.anon/maps` grows without bound. | `anon.py --prune-maps DAYS` lists the maps older than DAYS and deletes them only with `--yes` (dry run by default); `0` is refused, because "older than 0 days" would mean "delete everything, including the map written a second ago". Documented in the skill. | `tests/test_anon.py::CliTest::test_prune_maps_lists_and_deletes_only_with_yes` |
| 11 | The slim image (`WITH_CONVERTER=0`) existed only as a build arg. | A `slim` compose profile (`make up-slim`, port `${ANON_SLIM_PORT:-1408}`), and the image no longer ships `convert.py` when the converter is off — so `/api/state` answers `converter: false` honestly instead of offering docx/pdf uploads that then fail. | Verified by building and running the slim image: `converter: False`, no `/app/convert.py`, no `anydoc`. Smoke gate asserts the profile exists. |

### Found while closing (and closed)

- **Docs drifted from the code, in all three numbers that matter**: `DESIGN.md` said "uploads
  32 MB" (the cap is 160 MB) and "converter output 32 MB", the skill said "files over 8 MB are
  blocked" (the cap was 2 MB), and `DESIGN.md` said the map tag is "4 hex chars" (`TAG_DIGITS`
  is 6). All corrected in this commit. The class of defect is not closed — see the open list.
- **`sync-from-live.sh` claimed to mirror `docs/DESIGN.md`, which its own loop never copied** (and
  the direction of `requirements-anydoc.txt` is repo → live, the opposite of everything else).
  Comment corrected, and the pin is now copied explicitly.
- **The guard's cap was already dangerous, not conservative**: 0.15 MB/s made the old 2 MB cap
  13.7 s against a 15 s timeout. The next dictionary entry would have made every read time out —
  and a timeout used to disable the guard for the session.

### Found in the post-fix sweep (and closed)

A second pass over the code, hunting the *classes* of the first eleven rather than the items:

- **Silent truncation, twice more.** `findings` in `--check`/`--audit`/`/api/audit` was capped
  (20/50) with no flag, and `/api/maps` capped the list at 100 while `/api/state` counted through
  that capped list — 300 maps were reported as 100. The listings are still capped, the totals are
  not, and the truncation is declared (`findings_truncated`, `truncated`) and shown in the UI.
- **Dead code**: `looks_binary()` (a one-line wrapper over `sniff`, no callers) and `import json`
  in `convert.py`.
- **`anon.py`'s exit codes docstring never listed 4** (`--audit`: near-miss candidates).
- **The address corpus test leaked its temp directory.**
- **CI installed the converter by version**, not through the pinned requirements file, so CI could
  run a different wheel than the image ships. It now uses `--require-hashes -r requirements-anydoc.txt`.
- **A map file is operator-editable input, and the class took FOUR rounds — so the answer is
  structural, not a fifth patch.** Two more shapes survived the first fix (an entry VALUE that is
  null, and a `source` string with an embedded NUL reaching `Path()`), and a deeply nested request
  body raised `RecursionError` out of `json.loads` instead of a `ValueError`. Instead of guarding
  the next instance, the map is now validated ONCE at the boundary: `deanon._validate_entries`
  checks every entry (key string, value object, `original` string-or-null) and both readers —
  `load_map` (CLI restore, reveal, deanonymize) and `load_map_metadata` (the UI listing) — go
  through it, with the listing turning any failure into `unreadable` so no file can 500 a read.
  `_read_json` also maps `RecursionError`/`UnicodeDecodeError` to a 400. This is the point where a
  second round of instance patching would have been the wrong method. Completing it took one more
  pass (the first boundary validation was not applied to every entrance): the map file and the
  request body now share `anon.read_json_object`, `catalogs: 5` is a `ValueError` instead of a
  `TypeError` out of an iteration, and the listing validates entries WITHOUT copying them (`_count_entries`) —
  a count is not worth O(N) allocations per map.
- **A map file is operator-editable input, so every JSON read is now validated by shape AND type.**
  Closed one step further than the first report, after the same class reappeared twice: a top-level
  non-object map 500'd the listing, then `{"entries": 5}` did it again through `len(5)`, and the
  same shape reached `load_map` (reveal/deanonymize/CLI, where it escaped as an `AttributeError`
  traceback instead of exit 2). All four `json.loads` call sites in the project were enumerated
  and closed, including the request body (`[]` was a 500, now a 400).
- Caught by a second adversarial review of the sweep itself: the truncation flag was claimed for
  `/api/audit` but only implemented in the CLI (the web endpoint has its own findings loop);
  `/api/maps` built `total` and the listing from two separate directory listings (a concurrent
  write could make `truncated` false while the list was capped); and an unreadable map was skipped
  from the listing while still counted in `total`. All three fixed, with tests.

## Closed in the third pass (2026-09-22)

| # | What it was | How it is closed | Evidence |
|---|---|---|---|
| 23 | False-positive sweep over a real corpus — the prerequisite of 20. | `scripts/fp-sweep.py` scans a known-**clean** corpus (this repo, the Pi extensions, the installed Pi package) with the real `detect()` and aggregates every match per rule type. Private infrastructure configs are excluded by default: they hold legitimate real values, so a match there is a true positive, not a false one. Dictionary types are reported separately; a type that is a built-in rule is never reclassified as dictionary, so a pattern false positive cannot be hidden. | Clean corpus: **48 files, 85 pattern matches** — HOST 40, URL 18, INDIRIZZO 9, IP 6, KEY 5, EMAIL 5, TARGA 1, TEL 1. The signal that mattered was KEY. |
| 20 | The KEY rule redacted ordinary code (calls, member references) — the friction of this repository. | The assignment rule now rejects a right-hand side that is a CALL (regex lookahead `(?!\()`, kept whole by `(?![…])` against backtracking). A DOTTED value is deliberately KEPT: `password = my_secret.phrase` can be a real password, and a missed secret is worse than a block (an adversarial review rejected an earlier dotted-underscore heuristic as an unjustified shape-classifier). | KEY on the clean corpus went **11 -> 5** (the CALL shapes are gone); the 5 are two doc comments in `anon.py` plus three code references it keeps. Tests: `RoundTripTest::test_code_calls_are_not_secrets`, `::test_a_dotted_value_is_still_redacted`, plus the leak cases added to `test_real_secret_shapes_are_redacted`. |

## Closed in the fourth pass (2026-09-22)

The pass that made the guard usable instead of merely correct.

| # | What it was | How it is closed | Evidence |
|---|---|---|---|
| 31 | **The guard's verdict cache ignored `allow.txt`.** A path already cached as "sensitive" stayed blocked for the rest of the session after `~/.anon/allow.txt` was edited **by hand** — the documented way to un-block a path. `/anon-allow` cleared the cache (it goes through the extension); a hand edit did not, and that is the common case. Found while opening Pi's own documentation, which *is* allowlisted. | The cache key now carries a fingerprint of `allow.txt` (mtime + size) and of the session globs, so an edit invalidates the stale verdict. | Reproduced first: `anon.py --check` answered `allowed: true` while the guard kept blocking, and a sibling file under the same glob read fine. The live extension only reloads at the next Pi start, so the runtime confirmation of the fix is the next session's first read — declared, not assumed. |
| 32 | **A binary document read through Pi was a dead end.** The guard blocked it (correctly: unscannable ≠ clean), but the user had to run three manual steps the guard knew better than they did — and the friction of a dead end is what pushes toward `--anon-guard=off`, i.e. losing every protection. | `---anon-guard-auto=ask\|on\|off` (default `ask`): the guard converts + anonymizes in a local subprocess, writes the redacted Markdown in `~/.anon/auto/` (0600) and **rewrites the read** onto it; the model gets placeholders plus a banner. `ask` without a UI blocks; any failure falls back to the block, so the fail-closed property is unchanged. | DEC-0014 (supersedes DEC-0011, which stays as the historical record). `scripts/check-anon-guard.cjs`: **24 → 44** checks, including missing converter → block, `ask` with no UI → block, `ask` + "no" → block, a rejecting dialog → block, converter exit≠0 / empty / beyond the cap → block, the banner, and `off` unchanged. |
| 33 | **One dictionary file for everything.** People, companies and generic entries shared `entities.txt`. | Split by kind — `entities.txt` (generic), `people.txt` (`@type PERSONA`), `clients.txt` (`@type AZIENDA`) — all optional and read together; `--entities PATH` is repeatable. The web UI's Dizionario tab addresses the three files (`/api/entities?file=…`). | Tests `test_default_dictionaries_are_merged`, `test_entities_flag_is_repeatable`, `test_a_missing_explicit_dictionary_is_an_error`, `test_named_dictionaries_are_addressable_and_unknown_ones_are_refused`; the live migration was verified **equivalent** (2 entries, same types) with a backup. |
| 34 | **"The engine makes no network call" was a claim, not a gate.** One `import requests` would have made it false with every test still green. | `OfflineContractTest`: an AST **canary** on `anon.py`, `deanon.py` and `convert.py` — no STATIC import of a network stack (submodule-precise: `urllib.parse` and `http.cookies` stay importable), and no `subprocess` in the two document paths. | Proven to bite: injecting `import socket` into `anon.py` fails with `anon.py imports a network stack: ['socket']`; removed afterwards. `python3 tests/test_anon.py` — 92 tests then, 125 today. The adversarial review of the first draft cut it down to size: it had claimed to verify the *behaviour* while checking an import denylist (`__import__`, transitive imports and `convert.py --install`'s pip all pass), and its root-level `urllib`/`http` entries blocked legitimate parsing. The claim now matches the mechanism, and DEC-0012 §2 listing `convert.py` among the engine files while `--install` uses pip is left visible here instead of papered over. |

| 35 | **`@context` was sticky for the rest of the file**, so narrowing a context meant reordering the entries — and the ordering rule was nowhere written down. | `@context off` closes the block; the rule ("a directive applies to the entries that FOLLOW it, and a later one replaces it") is now stated in the parser's docstring, `catalogs/README.md` and `docs/DESIGN.md`. | `DirectivesTest::test_context_off_closes_the_block`, `::test_a_later_context_replaces_the_earlier_one`. |
| 24 | **The 6-hex tag was reasoned about, not measured** — and `SECURITY.md` promised a collision "stays negligible". | Measured (`scripts/tag-collision.py`): with 6 hex alone a collision has ~3% probability at 1 000 maps and ~55% at 5 000 (the expected collision count reaches 1 at ~5 800). The guess is gone: the tag is allocated against the tags **already in use** by the maps on disk, and the security claim is corrected. | `TagAllocatorTest` (retry + widening that stays inside the placeholder syntax, `existing_tags` skipping unreadable maps, a generated tag is never one of the existing ones); the script prints the curve. |
| 27 | **A gate on the numbers in the docs** — the 8 MB/2 MB, 4-hex/6-hex and "negligible collision" drifts were all found by hand. | `scripts/check-doc-numbers.py`, wired into `make test`: 16 claims binding a constant to the sentence that reports it (version, tag width *and every placeholder example*, upload cap, converter cap and timeout, rate limit, the near-miss bounds in the UI copy, the guard's cap when its source is reachable). History (CHANGELOG, OPEN-ISSUES) is deliberately out of scope. | First run caught 5 real drifts (4-hex examples in README/DESIGN while the engine writes 6). A missing guard source prints a visible SKIP, never a silent pass. |
| 12 | **`/deanon` in Pi**, symmetric to `/anon`. | `/deanon <file> [<map|map-id>]` restores into a FILE and reports the path. It does **not** paste into the editor: the symmetric behaviour would have put the real values into the model context, undoing the guard. The map is deduced from the `output` recorded in each map; when that is not exactly one, it refuses to guess. | Guard harness: `/deanon` registered, writes the file, finds the map by `output`, accepts a map id, refuses with zero and with two candidates, never calls `pasteToEditor`, and the notification carries no real value. |

### Measured, and it changes a plan (not closed)

The dictionary-size sweep above came out of trying to size item 14. It is worth stating plainly
because it moves the boundary of what the guard's cap means: the candidate pass costs roughly
**linear in the dictionary size**, so "12 MB / 20 s" is a statement about a few hundred entries,
not about the engine. Numbers are in the to-do rows for 14 and 26; `scripts/bench-check.py`
reproduces them offline against a synthetic dictionary (no real names).

### Corrected after the adversarial review of 12 / 24 / 27 (and closed)

- **The tag scan covered only one directory.** `anon.py --map /tmp/x.map.json` scanned `/tmp`, so it
  could draw a tag an existing `~/.anon/maps` map already used — while the prose promised the check.
  `allocate_tag` now takes the list of directories and the CLI passes both; `TagAllocatorTest` pins
  it with a deterministic `urandom`.
- **The web server allocated without a lock**, so two concurrent requests could draw the same tag —
  it is a threaded server, so this is not hypothetical. Allocation and map write are now serialised.
- **`@context no` / `@context 0` were silently read as "off"** (they shared the `_FALSE` list with
  `@stem`), so a gate the operator wrote would have been dropped while over-redacting. Only the
  exact token `off` clears the context now.
- **The doc-numbers gate could pass on the WRONG number**: every claim was a bare substring, so a
  value appearing elsewhere satisfied it — and both the upload cap and the converter cap are
  160 MB, in the same file. Each claim is now anchored to the sentence that reports it, and the
  quoted collision percentages are bound to the birthday formula.
- **The smoke gate hard-coded the 6-hex width** (`grep '[0-9a-f]{6}'`), so a widened 8-hex tag would
  have failed a check about tagging. It accepts 6-8 now, matching the placeholder syntax.
- **A `/deanon` happy path that did not exist**: matching the map on the recorded `output` only
  works when restoring the redacted file itself, while the documented workflow restores the
  FINISHED document. It now reads the tag out of the document first, and falls back to `output`.
- **Rejected, with reason:** that the `/deanon` notification leaks a name because a path can carry
  one. `notify` is the operator's toast, not the model's context: the operator chose the file, and
  the command has to say where the real values went. Deliberately not redacted.

### Found while closing 32 (and closed)

- **A missing dictionary file made the engine exit 2 with no JSON**, which the guard reads as "engine broken" and turns into a **fail-open for the session** — a leak introduced by making "no dictionary" a hard error. Fixed: no dictionary is not fatal (`--check --json` keeps emitting valid JSON, the pattern rules still run); only an explicit `--entities` that does not exist is an error. Covered by `test_no_dictionary_at_all_still_works` and `test_an_empty_entities_flag_is_an_error`.
- **The guard's runtime harness read the repo mirror, not the live file**: an edit to `~/.pi/agent/extensions/anon-guard.ts` was silently untested (and 12 new checks appeared to fail on old code) until the two were synced. They are two copies of one artifact, kept equal by hand and checked with `sha256sum`; step 19 vendors them so the invariant stops being manual.

### Corrected after the adversarial review of 32 (and closed)

The review (a different model) found the fail-open **before** the commit, which is the whole point of running it:

- **The remediation shared the CHECK path's swallow-all `catch`.** The `tool_call` handler wraps `checkFile` — and, with this change, `tryAutoRemediate` — in `try { … } catch {}`, which exists so a broken engine fails *open* (DEC-0011). A remediation that **throws** (the realistic case: the confirmation dialog rejecting) was swallowed too, so the handler returned `undefined` and the read proceeded on the **original binary**, un-redacted. DEC-0014 §5 says a failed remediation blocks. Fixed with a wrapper that can only return a block verdict; the harness now drives a rejecting dialog and **fails without the fix** (verified by re-injecting the old code: exactly that one check fails).
- **stdout was decoded per chunk**, so a multibyte character straddling a pipe-buffer boundary became U+FFFD — silently corrupting an accented document *before* the engine scanned it, and with it an accented entity the dictionary should have matched. The bytes are now concatenated and decoded once.
- **stderr had no cap** (only stdout did): a chatty child could grow the guard's memory unboundedly. Capped at 64 KB.
- **The intermediate Markdown (real values) stayed on disk forever** in `~/.anon/auto/`, with no prune counterpart. It is now removed after the run, whatever the outcome: the redacted copy and the map are what the operator needs, and `auto/` holds placeholders only.
- **Rejected, with reason:** hashing `allow.txt`'s *content* into the cache key instead of mtime+size. The dangerous direction (a glob removed from the allowlist leaving a cached "allowed") needs a same-size edit inside the same mtime tick; mtime is nanosecond-resolution here, and paying an I/O read on every check to close a window that does not open is the wrong trade. Declared, not silently dropped.

## Closed in the fifth pass (2026-09-22)

The pass that closed the smaller items — each one either a measurement or a refusal made explicit.

| # | What it was | How it is closed | Evidence |
|---|---|---|---|
| 15 | **Batch mode**: anonymize a folder in one command. | `anon.py DIR --batch` writes `*.redacted.*` next to each source and never over it. It skips binaries (never copied under a `redacted` name), anything over 12 MB, and its own outputs, so a second run cannot nest placeholders; a directory inside `~/.anon` is refused outright. `--batch --check` is the folder-shaped gate; `--batch --out DIR` keeps the source folder clean. | `BatchTest` — 13 tests (6 at the time of the fix, and the symlink cases came later): the writes, the skips, idempotence, `--dry-run` writing nothing, the gate's exit 1, `--out`, and the four refusals. |
| 16 | **`--dry-run`**: no preview of what a run would do. | Reports what WOULD be redacted, per type, and where it would land — allocating no tag and writing no file (`--check` answers "is it sensitive?"; this answers the question that comes first). | `BatchTest::test_dry_run_reports_the_plan_and_writes_nothing` and the batch dry-run test. |
| 25 | **Converter fidelity was declared "unknown"** — and it bounds everything downstream. | Measured: `scripts/convert-fidelity.py`, which proves each feature is present in the fixture before looking for it in the Markdown (a guessed signature is how a measurement lies). **11 of 17** features survive. Not carried: headers, footers, comments, tracked deletions, and the core properties (title, **author**). | The script prints the table. The consequence is stated in DESIGN 8 and the `anon` skill: those texts are never redacted, and they survive in the original `.docx` and in any PDF exported from it. |
| 28 | **`DESIGN.md` covered the engine and the web app at once.** | The web perimeter moved into `SECURITY.md`, where the threat model already pointed at it; `DESIGN.md` was renumbered and now states the engine only. One document per audience, no third file, no perimeter written twice. | DESIGN's section list is engine-only (1-8); the 20 doc-number claims still bind, three of them now anchored in SECURITY.md. |
| 29 | **No picture of the UI in the README.** | Captured with headless Chrome driving a NATIVE server on a temporary `ANON_HOME` seeded with synthetic data — never the container, which mounts the real `~/.anon`. The capture shows the placeholders, the per-type chips and the map that produced them. | `docs/brand/ui-anonymize.png`, referenced from the README's Web UI section. |
| 30 | **A skipped converter install was visible only in the log.** | The CI step records the outcome, emits a `::warning`, and appends a line to the run summary ("installed" or "NOT installed — the docx/pdf tests were skipped"); CI also runs the doc-numbers gate now. | `.github/workflows/ci.yml`, validated as YAML. |
| 22 | **`verify.py` could read a redacted copy of a decision AS the decision.** | `DEC-0012.redacted.md` sorts after `DEC-0012.md` and carries the same id, so the last one won and the real record was shadowed. `*.redacted.*` and `*.deanon.*` are now ignored. | Proven both ways: with the fix `DEC-0012` passes; a copy restored to the old condition reports `FAIL — missing evidence` from the decoy. The fix lives in the memory skill's `verify.py`, in the Pi-config backup repo. |
| 18 | **The portable workbench carried no guard.** | Guard, skill and runtime harness shipped into `pi-workbench`, with `scripts/check-anon.sh` comparing sha256 against the live config — and saying so, visibly, when it cannot compare. | pi-workbench `15499a7`; its gates pass (`check-config-docs.sh`, `check-extensions.cjs`, `check-skill-frontmatter.cjs`, `check-anon-guard.cjs`, `check-anon.sh`). The README entries for the port are written and sit uncommitted in that repo, next to unrelated in-flight work — deliberately not swept into that commit. |

### From a live report (2026-09-22)

Dropping a `.docx` on the Anonimizza tab did nothing: no button, no message, no progress. Two halves
disagreed — a drop cannot populate a file INPUT, the drop handler stored nothing for a document, and
the button read `#file-anon.files`. The picker worked, the gesture did not, and **no test noticed**
because `tests/ui_load_check.mjs` only proved that the script parses: it discarded every listener
(`addEventListener() {}`) and returned a fresh element per `getElementById`, so no gesture could be
driven and no post-gesture state could be read. Both are fixed in the harness, and the drop now has
four interaction checks. The same report asked for progress: the upload percentage comes from XHR
(the one thing `fetch` cannot report), the conversion phase shows elapsed time instead of inventing
a percentage, and the bar stays visible at least 700 ms. Verified by driving the running container
through CDP with a real `.docx`, not only in the unit checks.

### The container path — status (2026-09-22)

Done: the engine (`anon.py verbale.docx` -> `verbale.redacted.docx`, part by part, output verified,
`deanon` restores part by part), the UI (one redaction, two downloads: the `.docx` and the `.md`
derived from the redacted file), `--check` naming the findings while keeping `unscannable`, fixtures
proving docx/xlsx/odt/pptx in place, the doc sweep, and the adversarial review of the container pass
(the four defects it found are closed in `a6b284b`, below). Commit `bb45726` and after; DEC-0015.
Legacy `.doc/.xls/.ppt`, PDF and images remain refused by design.

### The structural-boundary refusal was too broad (2026-09-22, same day)

The fix for the MEDIUM finding below refused a real document on the first try: a footer where the
company name is split by a `<w:tab/>` between the name and its legal form. The rule had been stated
as "only run-level tags may be crossed", which is wrong in the other direction — a tab, a line break,
a bookmark, a hyperlink or a content control are all INSIDE one text container, and an ordinary
letterhead uses them. An inline set (then called `INLINE_TAGS`, now `INLINE_ELEMENTS`) listed what a
word processor actually splits a value with,
ANY other tag is structural, and the refusal names the tag (`w:p`, `dc:title`) so the operator has
something to act on. Four fixtures (tab, break, bookmark, content control) fail if the list is
narrowed back to the run-level set.

**Second correction, same day, same mechanism** — and the rule that came out of it. The next real
document was refused again, this time on `<w:rFonts/>`, `<w:spacing/>`, `<w:sz/>`, `<w:szCs/>`:
those are the run PROPERTIES Word writes on every run, and a per-tag classifier cannot see that they
open nothing. Two failures of the same mechanism in one day is the signal to change the mechanism
rather than the list, so the classification now compares the CONTAINER of the two fragments: the
walker records the element path (names and instance ids) each fragment lives in, and joining is safe
when the paths diverge only on inline elements. Instance ids are what make it work — by name alone,
two runs of one paragraph and two paragraphs look identical. Tests fail if the comparison downgrades
to names, and if the refusal disappears.

### Hunt for the same class of defect, after the second correction (2026-09-22)

The operator asked whether other bugs of the same kind existed. Three were found and fixed.

1. **The mirror direction had the mirror defect, and a test ENCODED it.** `deanon`'s
   `repair_split_placeholders` distributes the value into the first fragment and empties the others
   with no boundary check — across a paragraph boundary it would move the value into the first
   paragraph and delete the second one's text. Its test claimed "Word splits a placeholder across
   RUNS" and its fixture put the halves in two PARAGRAPHS, asserting the destructive behaviour as
   correct. The classifier now runs in both directions from the same code (`boundary_offenders`), the
   fixture is a genuine run split, and a new test pins the cross-paragraph case: not repaired, exit 3,
   and the second paragraph's text intact. Verified by mutation.
2. **The inline set was extended, but less of it bites than I first claimed** — corrected after
   review attacked the claim rather than the code. Of the 8 corpus constructs (table cell, text box,
   tracked insertion, tracked deletion, field in the middle, math, hyperlink, block content control),
   only the MATH case (`m:r`) is a genuine behaviour change: in the other seven the first differing
   element was already `w:r` under the old set. The leaf text nodes (`w:delText`, `m:t`) and the
   self-closing markers (`w:fldChar`, `w:footnoteRef`, ...) can never be a difference at all — a
   self-closing tag is never pushed, and a leaf always sits under a run that differs first. They are
   harmless, not load-bearing. And `w:txbxContent` was never added, and must not be: a text box IS a
   container (see point 4). The corpus test is still worth having — it is what fails if the signature
   starts refusing a legitimate split.

3. **A part named as text but undecodable was skipped silently** — only `.xml`/`.rels` were protected,
   so a `.vml`, `.rdf` or `.txt` part that could not be decoded went through unscanned, and the
   verification (same view) would not have seen it either. Now refused in the redaction, and reported
   as `unreadable_parts` in the restore, where it also prevents `complete: true`.

**4. The boundary decision itself was still wrong, and adversarial review found a DESTRUCTIVE case.**
The first version compared the element paths and stopped at the first difference. A fragment inside a
text box nested in a run differed from the body first at `w:r` (inline, harmless) while the real
boundary — a `w:p` inside `w:drawing` / the legacy VML `w:txbxContent` — sat three levels deeper: the
check returned "safe", the value was rewritten, and the text box's text was DELETED. Reproduced for
both the DrawingML and the VML text box. Two more holes in the same decision: only the first and last
fragments were checked, so a value whose MIDDLE landed in another container passed (destructive in
both directions); and `m:oMath`/`m:oMathPara` counted as inline, so two equations counted as one text.

The decision is a **signature** comparison now — every ancestor that is not an inline element, for
EVERY fragment — which closes all three. Verified with the review's own reproductions: body + text box
→ refused; three fragments with the middle in a text box → refused (anon: exit 2, nothing written;
deanon: exit 3, `repaired 0`, the text box's text intact and no value placed in it); two equations →
refused; and the legitimate splits (two runs, tab, break, bookmark, content control, math within one
equation) still produce a redacted document.

Declared residuals of the sweep:

- embedded binary objects (`word/embeddings/*.bin`, an xlsx `vbaProject.bin`, media) are not scanned
  — a container inside the container;
- a part named as text that cannot be decoded degrades differently depending on the entry point: the
  redaction REFUSES and names the part, while `--check`, `--audit` and `--batch --check` answer
  `unscannable` / "container refused" — correct (fail-closed) but less specific. `--batch` now keeps
  the message; the two check modes deliberately keep the shape the Pi guard reads;
- `MARKUP_RE` (`<[^>]*>`) mis-tokenizes a tag whose ATTRIBUTE VALUE contains `>` (legal XML), and a
  CDATA block or comment containing one: the tail leaks into the visible text and shifts the offsets
  around it. The element NAME is still parsed, so the container signature stays right; what suffers is
  the text near such a construct, where a match could hide. Legal and rare in Office output, pre-existing,
  now written down instead of waiting to be discovered.

### Adversarial review of the container pass (2026-09-22) — every finding accounted for

Review by a different model on the engine + UI of the container pass. Four real defects, all fixed
in the same change, each with a test that FAILS against the old code (checked by re-injecting the
defect, not by assertion):

- **HIGH — deadlock on the PDF fallback.** `_anonymize_document_file` held `TAG_LOCK` and called
  `_anonymize_text`, which takes it again; `threading.Lock` is not reentrant, so one PDF hung that
  request and left the lock held for every later one. The fallback now runs outside the block; the
  test is a watchdog (mutated code fails it after 10 s).
- **HIGH — a UTF-16/32 part without a BOM was treated as binary**, so it was neither scanned nor
  verified and the check reported "0 leftovers" while the value was intact. Wide encodings are now
  recognised by byte pattern.
- **HIGH — no bound on decompression.** `zipfile` inflates a part before anyone can inspect it, and
  the per-character index multiplies it further; `--check` (the guard's `read` path) was exposed.
  Caps on part size, expansion ratio and total, plus the 12 MB scan budget for `--check`.
- **MEDIUM — a match spanning a structural boundary destroyed the other element's text** and stored
  the separator spaces in the map. Now classified and refused.
- MEDIUM — response amplification (base64 inside JSON, ~2.5x the file): the document is withheld
  beyond the upload cap and said to be. Residual, declared: below the cap the response is still
  built in memory.
- LOW — the `if not raw` guard sat after the allocation (unreachable, but the ordering was wrong);
  LOW — the map was written before the conversion, so a failed conversion left a map holding real
  values behind (now deleted with it); LOW — a doc comment claimed detection and verification used
  the same view: true per part, and the joined re-scan is deliberately stricter.

Both residuals are closed:

- the index no longer costs tens of bytes per character: the visible text is built from chunks and
  joined once, and the offsets live in an `array('i')`. Measured on a 16.2 MB XML part (12.6 MB of
  visible text): **peak RSS 544 MB -> 100 MB**, i.e. ~43 -> ~8 bytes per visible character, at ~23%
  more time — reproduced by `scripts/bench-index.py`, which runs both implementations in separate
  processes so the figure can be re-measured rather than trusted. The offsets are still linear in the text — that is what a mapping is — but the multiplier
  is no longer the problem;
- the document no longer travels inside the JSON response: it is published under
  `~/.anon/downloads/<token>/` (directory 0700, file 0600, pruned after an hour) and **streamed** from `GET /api/download/...` in
  64 KB blocks, so the server holds one block at a time instead of ~1.33x the file as a string on
  top of the file, the Markdown and the decoded copies. The docker smoke now downloads the document
  through that endpoint and inspects its parts.

## Closed in the sixth pass (2026-09-23)

The vendor list: it exists, it was reviewed twice, and the rule that makes it usable is now enforced.

| # | What it was | How it is closed | Evidence |
|---|---|---|---|
| 37 | **The two-block rule of `catalogs/vendors.txt` was a prose convention, not an enforced invariant.** Two adversarial reviews of the first version moved FIFTEEN names between the blocks — six ordinary words in the case-insensitive one (`gigabyte`, `red hat`, `avast`, `veritas`, `trend micro`, `western digital`), three needlessly case-sensitive (`aruba`, `kingston`, `eaton`), `cisco` (a fish, and an enum-listed word) put in the wrong block BY the fix itself, `okta` (a cloud-cover unit), and `ibm`/`amd`/`hpe`/`apc` kept case-sensitive on a rationale that was simply false — they are neither words nor identifiers, which made `un server ibm` a MISS. | `VendorsCatalogTest` reads the two blocks out of the FILE and enforces BOTH directions: every case-sensitive name must record the ordinary word it collides with (`WORD_COLLISION`), and no block-2 name may be one of those words or a word of the OS dictionary. The OS list alone is not enough (no `gigabyte`, `google`, `veritas`, `siemens`, `okta`, `hp`, `intel`, `xerox`, `arista` in it), which is why the recorded word is the primary evidence and the sweep is the net. | Proven to bite by mutation, four ways: a word moved into block 2, a recorded word deleted, an unjustified case-sensitive name added, and a dictionary word (`Onyx`) appended to block 2 — `test_every_case_sensitive_name_records_its_ordinary_word`, `test_no_case_insensitive_name_is_an_ordinary_word`, `test_the_system_word_list_finds_no_collision_either`. The sweep SKIPS VISIBLY when there is no dictionary to read. |

Also in this pass: the list grew from 116 to **164 entries** (the international vendors an
infrastructure report names, the OT/industrial ones, and the main Italian software houses),
chosen by the same rule and checked by the gate above — the eight additions that are ordinary
words (`Ruckus`, `Arista`, `Oki`, `Zebra`, `Moxa`, `Sage`, `New Relic`, `Open Text`) carry their
word, and the cost was re-measured at the new size (`scripts/bench-check.py --entities 164 --mb 5`,
1.82 MB/s against the guard's 12 MB / 20 s). Declared and NOT closed: product and model names
(`FortiGate`, `PowerEdge`, `BIG-IP`) remain out of scope by design, and the sentence-initial
Italian elision (`Dell'azienda risulta…`) remains a KNOWN false positive that the format cannot
express an exclusion for.

## To-do — the whole list, in the order I would take it

One ordered list. The **IDs are stable references, not an order**: 1-11 are the items closed above,
12-22 were the original open list, 23+ were added by the post-fix sweeps. **13 is withdrawn**
(per-client profiles: not interesting at the moment) and its number is left unused on purpose, so an
old reference can never point at a different item.

| Order | # | Next action | Why now | Size |
|---|---|---|---|---|
| 1 | 20 | **Tighten the KEY rule** — CLOSED in the third pass (see below). |  | closed |
| 2 | 23 | **False-positive sweep over a real corpus** — CLOSED in the third pass (see below). |  | closed |
| 3 | 21 | **`@context off`** in the dictionary, plus a line documenting the ordering rule — CLOSED in the fourth pass (see below). |  | closed |
| 4 | 24 | **Tag collision**: verify empirically over N maps that 6 hex digits are enough, or derive the tag from the map id — CLOSED in the fourth pass (see below). |  | closed |
| 5 | 17 | **Optional local-model detector** (DESIGN §7) — **CLOSED 2026-09-23.** The seam, the approval panel, the shipped model and the measurement are all done: `make model` downloads Qwen2.5-3B-Instruct Q4_K_M (the published size and sha256 matched on the first completed download, so they are verified), `docker-compose.model.yml` runs it as a sidecar sharing the UI's network namespace (the loopback rule holds unchanged, nothing is published), and the panel answers through `/api/suggest`. **Measured end to end**: a 570-character Italian report → 37.3 s, 5 proposals (Ancona, Milano, Prato, Bologna, one address), **4 of which the deterministic engine does not find**. Declared limit: the latency (21.6 tokens/s prompt, 6.7 generating) is why the panel is a separate step, not part of the upload. | The structural answer to contextual references — the biggest declared hole. | closed |
| 7 | 12 | **`/deanon` command in Pi**, symmetric to `/anon` — CLOSED in the fourth pass (see below). |  | closed |
| 8 | 18 | **Ship the `anon` skill and `anon-guard.ts` into `pi-workbench`** (with a sync script + a sha256 check, so the copies cannot drift) — CLOSED in the fifth pass (see below). **Vendoring a copy into THIS repository stays rejected.** |  | closed |
| 9 | 27 | **A gate on the numbers in the docs**: read the constants from the code and assert they still appear in README/DESIGN — CLOSED in the fourth pass (see below). |  | closed |
| 10 | 15 | **Batch mode**: anonymize a directory in one command — CLOSED in the fifth pass (see below). |  | closed |
| 11 | 16 | **`--dry-run`**: list what *would* be redacted, per type, writing nothing — CLOSED in the fifth pass (see below). |  | closed |
| 12 | 28 | **Split `docs/DESIGN.md`** (engine vs UI) — CLOSED in the fifth pass (see below). |  | closed |
| 13 | 25 | **Converter fidelity**: measure what `.docx → Markdown` loses (tables, headers, tracked changes, metadata) — CLOSED in the fifth pass (see below). |  | closed |
| 14 | 29 | **A screenshot of the UI in the README**, from an asset safe to publish — CLOSED in the fifth pass (see below). |  | closed |
| 15 | 30 | **CI**: make the converter-install skip visible in the run summary, not only in the log — CLOSED in the fifth pass (see below). |  | closed |
| 16 | 22 | **`verify.py` must ignore `*.redacted.*`** when scanning decisions — CLOSED in the fifth pass (see below). |  | closed |
| 17 | 26 | **Guard throughput vs dictionary size** — **CLOSED 2026-09-23.** The first half built the index once per run and resolved a position through a sub-index on the SECOND token; the second half replaced the locator. The first-token alternation was 8.8 s of 9.1 s on a real 5 MB document with the catalogs (0.45 MB/s): its sources are common words and `re` retries every branch at every offset. Sources made of word characters are now located by walking the word runs of the text with dict lookups; only a source holding a non-word character (`D-Link`, `Hyper-V`) still needs an alternation, and so does a dictionary small enough that the word walk's per-token floor costs more than it saves (measured crossover between 64 and 80 sources, threshold 72 — `scripts/bench-scan.py --crossover`). **Measured**: locator 0.46 → 3.85 MB/s, whole scan 0.46 → 3.21 on 5 MB with the catalogs, same hit count; the guard's own path (6 sources) is unchanged at 0.090 s for 2 MB of real code (22.3 MB/s), so its 12 MB / 20 s margin stands. The adversarial review of this change found two HIGH defects of one pre-existing class, both fixed: the alternation used non-overlapping `finditer`, so `B-C` inside `a-b-c` (declared next to `A-B-C`) and `Link` inside `D-Link` were never probed — 1987 of 2000 random `X-Y-Z`/`Y-Z` pairs — and below the threshold the word sources inherited that miss, which is the guard's path; `_alternation_hits` now probes the positions inside each match (a zero-width lookahead would have been simpler and cost 17% on the guard's path). The review also corrected the bench (it compared different scopes), the corpus claim (synthetic prose, not source code) and the test oracle (its sort key omitted the priority). Proved equivalent to the reference by `test_the_word_run_locator_matches_the_reference_on_every_difficult_shape` over both regimes, with four mutations shown to bite. The test also found a PRE-EXISTING false negative: `re` folds `İ`/`I`/`ı` together while `casefold` does not, so an entry `İpek` was missed in a document spelling it `ipek` — fixed by keying on a fold proved (exhaustively, over every cased character) to be at least as coarse as `re.IGNORECASE` (`test_the_locator_fold_covers_everything_ignorecase_matches`). | The catalogs are 8 000 municipalities plus vendor and product names: a real document hits this on every page. | closed |
| 18 | 36 | **A symlinked FILE inside a `--batch` tree is followed even when the target is outside the tree or inside the private store** — **CLOSED**: the walk resolves each candidate and skips it, saying which reason, when the target leaves the tree or touches the private store; `test_a_symlink_out_of_the_tree_is_skipped_not_followed` and `test_batch_check_also_skips_a_symlink_out_of_the_tree` cover both paths. |  | closed |
| 19 | 19 | **Phone-prefix catalog** — **no action, declared**: it would compete with the phone rule and fragment numbers, which is worse than not having it. Revisit only with a real use case. |  | no action |
| 20 | 40 | **Run the private-data backup and TEST a restore** (`scripts/backup-private-data.sh`). It exists, it is verified in the same run it writes, and it has never been run by the operator: the transcripts and the anonymizer's dictionary and maps are in no repository, so this is the only copy. Owner: the operator, not the code. |  | **OPEN** |
| 20 | 37 | **The two-block rule of `catalogs/vendors.txt` is a prose convention, not an enforced invariant** — CLOSED in the sixth pass (see above): `VendorsCatalogTest` fails a name in the wrong block, in both directions, and sweeps block 2 against the OS dictionary. Two adversarial reviews of the first version moved FIFTEEN names — six ordinary words in the case-insensitive block (`gigabyte`, `red hat`, `avast`, `veritas`, `trend micro`, `western digital`), three needlessly case-sensitive (`aruba`, `kingston`, `eaton`), `cisco` put in the wrong block BY the fix itself, `okta` (a cloud-cover unit), and `ibm`/`amd`/`hpe`/`apc` kept case-sensitive on a rationale that was false, which made `un server ibm` a MISS. |  | closed |
| 21 | 38 | **The web UI in English as well as Italian.** CLOSED 2026-09-23: the header carries a language selector, Italian is the default and the source (the text stays in `web/index.html`), English lives in `web/i18n.js` keyed by a CSS selector per element — a paragraph that mixes text with `<strong>`/`<em>`/`<code>` is replaced whole, because word order differs between languages and translating a text node would produce English that does not compose. The choice persists in `localStorage` (`anon-lang`), `applyLanguage` restores Italian from a snapshot taken at load, `app.js` keeps an Italian fallback so a missing `i18n.js` cannot break the page, and a test fails when a dictionary key no longer matches an element. 50 keys, 41 web tests. **Declared limits, corrected after the completion pass**: the placeholders and the tooltips of a translated element ARE translated now, and the server's own messages were already English (the engine raises English exceptions because the code is English). What stays Italian is deliberate: paths, format lists and the language names written in their own language. | The README stated it plainly, so an English-speaking reader met an Italian interface at the first click. | closed |
| 22 | 39 | **CI: bump the action majors GitHub now annotates** — **CLOSED 2026-09-23**: `actions/checkout@v4 → v7`, `actions/setup-python@v5 → v7`, `actions/setup-node@v4 → v7`; the run after the push is green and the annotations are gone. No other repository of the fleet has a workflow (checked). |  | closed |

### Hygiene note kept from this pass

`docs/OPEN-ISSUES.md` itself used to be guard-blocked (it quoted an address and a name/literal pair),
so a session on this repository needed a redacted copy to read its own to-do list. The examples are
now written so the file stays readable by the agent, and the concrete shapes live in the git history.


## Verified at the end of the 2026-09-22 pass (history, not open)

- The new guard, end to end, in a fresh Pi process: a clean file is readable, a sensitive file is
  blocked with its type summary, a 14.4 MB file is blocked with the new cap. **The guard change takes
  effect at the next Pi start**: the session that made it still ran the old 2 MB guard.
- The container is rebuilt and running the current code: `/api/maps` now answers `total` and
  `truncated` (the shape added in this pass), `/api/state` reports `converter: true`, the API still
  refuses a request without the token (403), and the image was built through `--require-hashes`.
- The slim image (`make up-slim`) is verified separately: no `/app/convert.py`, no `anydoc`, and
  `/api/state` honestly answers `converter: false`.
- `deanon` cannot restore a document with another run's map (CLI and browser DOM).
- Suites: **79 engine tests, 24 web tests**, the UI load check, and the docker smoke — green, on the
  live tree (`~/.anon`), on the repository tree, and inside the container.
- Every JSON entrance (map files, request bodies, filter fields) was enumerated and validated at one
  boundary after the fourth round of the same defect class; the last adversarial pass returned
  "class closed at every entrance: yes".
