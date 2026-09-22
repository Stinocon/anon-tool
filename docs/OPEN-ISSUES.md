# Open issues — for the next pass

Second pass, 2026-09-22. The first eleven items of this list are **closed**, with the evidence next
to each one; what is still open follows below, in roughly the order I would tackle it.

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
| 23 | False-positive sweep over a real corpus — the prerequisite of 20. | `scripts/fp-sweep.py` scans a known-**clean** corpus (this repo, the Pi extensions, the installed Pi package) with the real `detect()` and aggregates every match per rule type. Private configs (Home Assistant, MikroTik) are excluded by default: they hold legitimate real values, so a match there is a true positive, not a false one. Dictionary types are reported separately; a type that is a built-in rule is never reclassified as dictionary, so a pattern false positive cannot be hidden. | Clean corpus: **48 files, 85 pattern matches** — HOST 40, URL 18, INDIRIZZO 9, IP 6, KEY 5, EMAIL 5, TARGA 1, TEL 1. The signal that mattered was KEY. |
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
| 22 | **`verify.py` could read a redacted copy of a decision AS the decision.** | `DEC-0012.redacted.md` sorts after `DEC-0012.md` and carries the same id, so the last one won and the real record was shadowed. `*.redacted.*` and `*.deanon.*` are now ignored. | Proven both ways: with the fix `DEC-0012` passes; a copy restored to the old condition reports `FAIL — missing evidence` from the decoy. The fix lives in `pi-customization/skills/memory/verify.py`. |
| 18 | **The portable workbench carried no guard.** | Guard, skill and runtime harness shipped into `pi-workbench`, with `scripts/check-anon.sh` comparing sha256 against the live config — and saying so, visibly, when it cannot compare. | pi-workbench `15499a7`; its gates pass (`check-config-docs.sh`, `check-extensions.cjs`, `check-skill-frontmatter.cjs`, `check-anon-guard.cjs`, `check-anon.sh`). The README entries for the port are written and sit uncommitted in that repo, next to the operator's in-flight offload-rules work — deliberately not swept into my commit. |

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
proving docx/xlsx/odt/pptx in place, and the doc sweep. Commit `bb45726` and after; DEC-0015.

Left: the adversarial review of the container pass (mandatory, different model). Legacy
`.doc/.xls/.ppt`, PDF and images remain refused by design.

### The structural-boundary refusal was too broad (2026-09-22, same day)

The fix for the MEDIUM finding below refused a real document on the first try: a footer where the
company name is split by a `<w:tab/>` between the name and its legal form. The rule had been stated
as "only run-level tags may be crossed", which is wrong in the other direction — a tab, a line break,
a bookmark, a hyperlink or a content control are all INSIDE one text container, and an ordinary
letterhead uses them. `INLINE_TAGS` now lists what a word processor actually splits a value with,
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

## To-do — the whole list, in the order I would take it

One ordered list. The **IDs are stable references, not an order**: 1-11 are the items closed above,
12-22 were the original open list, 23+ were added by the post-fix sweeps. **13 is withdrawn**
(per-client profiles: not interesting at the moment) and its number is left unused on purpose, so an
old reference can never point at a different item.

| Order | # | Next action | Why now | Size |
|---|---|---|---|---|
| 1 | 20 | **Tighten the KEY rule** — CLOSED in the third pass (see below). | | — |
| 2 | 23 | **False-positive sweep over a real corpus** — CLOSED in the third pass (see below). | | — |
| 3 | 21 | **`@context off`** in the dictionary, plus a line documenting the ordering rule — CLOSED in the fourth pass (see below). | | — |
| 4 | 24 | **Tag collision**: verify empirically over N maps that 6 hex digits are enough, or derive the tag from the map id — CLOSED in the fourth pass (see below). | | — |
| 5 | 17 | **Optional local-model detector** (DESIGN §7): a localhost endpoint that *suggests* candidates which a human approves. | The structural answer to contextual references — the biggest declared hole. The engine stays the only writer, so determinism is untouched. | large |
| 7 | 12 | **`/deanon` command in Pi**, symmetric to `/anon` — CLOSED in the fourth pass (see below). | | — |
| 8 | 18 | **Ship the `anon` skill and `anon-guard.ts` into `pi-workbench`** (with a sync script + a sha256 check, so the copies cannot drift) — CLOSED in the fifth pass (see below). **Vendoring a copy into THIS repository stays rejected.** | | — |
| 9 | 27 | **A gate on the numbers in the docs**: read the constants from the code and assert they still appear in README/DESIGN — CLOSED in the fourth pass (see below). | | — |
| 10 | 15 | **Batch mode**: anonymize a directory in one command — CLOSED in the fifth pass (see below). | | — |
| 11 | 16 | **`--dry-run`**: list what *would* be redacted, per type, writing nothing — CLOSED in the fifth pass (see below). | | — |
| 12 | 28 | **Split `docs/DESIGN.md`** (engine vs UI) — CLOSED in the fifth pass (see below). | | — |
| 13 | 25 | **Converter fidelity**: measure what `.docx → Markdown` loses (tables, headers, tracked changes, metadata) — CLOSED in the fifth pass (see below). | | — |
| 14 | 29 | **A screenshot of the UI in the README**, from an asset safe to publish — CLOSED in the fifth pass (see below). | | — |
| 15 | 30 | **CI**: make the converter-install skip visible in the run summary, not only in the log — CLOSED in the fifth pass (see below). | | — |
| 16 | 22 | **`verify.py` must ignore `*.redacted.*`** when scanning decisions — CLOSED in the fifth pass (see below). | | — |
| 17 | 26 | **Guard throughput vs dictionary size.** The cap is derived from a measurement, but that measurement assumed a small dictionary. Measured today (`scripts/bench-check.py`, 2 MB corpus): 200 entries 1.60 MB/s · 1 000 → 0.93 · 4 000 → 0.37 · 7 900 → 0.20. At 1 000 entries the 12 MB cap already needs 13 s of a 20 s budget; at 4 000 it exceeds it. Either measure at run time or size the cap against a declared entry count. | 12 MB / 20 s were sized on this Mac with a few hundred entries; the margin is not a property of the guard, it is a property of the dictionary. | medium |
| 18 | 36 | ~~**A symlinked FILE inside a `--batch` tree is followed** even when the target is outside the tree or inside the private store.~~ **Fixed**: the walk resolves each candidate and skips it — saying which reason — when the target is inside the private store or leaves the scan root. A test with both kinds of link fails against the old code. The scope is the tree the operator named; a link is a way of naming something else. A HARD link is not covered (it is not dereferenceable): it resolves inside the root, so it is processed — benign, because the output is redacted and creating one needs write access to the tree, but it is the honest boundary of the fix. | `--batch` was fixed, but silently FOLLOWING a link is the same class of mistake: the tool processed a file nobody put in scope, and the private store's maps are exactly what must not leave it. | small |
| 19 | 19 | **Phone-prefix catalog: no action.** | Deliberately not shipped: it would compete with the phone rule and fragment numbers, which is worse than not having it. Revisit only with a real use case. | — |

### Hygiene note kept from this pass

`docs/OPEN-ISSUES.md` itself used to be guard-blocked (it quoted an address and a name/literal pair),
so a session on this repository needed a redacted copy to read its own to-do list. The examples are
now written so the file stays readable by the agent, and the concrete shapes live in the git history.


## Verified today (not open)

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
