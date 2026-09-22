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
| 3 | Address detection was noisy and blind at the edges: prose was redacted and common address forms were missed. Under the hood, three alternatives (`v.le`, `p.zza`, `n.`) were escaped twice in the pattern, so they matched a literal backslash and never fired; `G.` was not accepted in a name; `3/A` was cut off. | The rule was rebuilt with a shared source (one regex, `ADDRESS_RE`), abbreviations fixed, dots allowed inside names, the civic suffix part of the span — plus a validator that requires one capitalized name token, which is what separates `Via Roma 12` from `in via del tutto eccezionale, 3 volte`. | `tests/test_anon.py::AddressCorpusTest` (12 positive, 6 negative). Before: 6 misses and 3 false positives on that corpus. After: 0 and 0, with `via roma 12` declared as a deliberate miss (precision over recall). |
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
- Caught by a second adversarial review of the sweep itself: the truncation flag was claimed for
  `/api/audit` but only implemented in the CLI (the web endpoint has its own findings loop);
  `/api/maps` built `total` and the listing from two separate directory listings (a concurrent
  write could make `truncated` false while the list was capped); and an unreadable map was skipped
  from the listing while still counted in `total`. All three fixed, with tests.

## Open issues — for the next pass

### Bugs and correctness

| # | Issue | Severity | Note |
|---|---|---|---|
| 20 | **The KEY heuristic redacts ordinary code.** `token\s*[:=]\s*VALUE` matches `const TOKEN = window.ANON_TOKEN` and `cls.token = match.group(1)`, which is why the guard blocks this repository's own `app.js`, `tests/test_web.py` and (partly) `anon.py`. | medium | Needs a tighter rule that keeps real secrets: reject a value that is immediately followed by `(`/`[`, or a dotted attribute chain. Any loosening has to be measured against the false-positive corpus in `tests/test_anon.py` — the failure mode of getting this wrong is a missed secret, not a blocked read. |
| 21 | **A `@context` cannot be switched off.** It applies to every following entry, so a dictionary that mixes context-gated entries (a city catalog) with plain ones has to keep them in the right order — there is no `@context off`. | low | A dictionary is the operator's file: an invisible ordering constraint is the kind of thing that silently under-redacts. Proposal: `@context off` (and document the ordering rule). |
| 22 | The old structural issue is still open: `verify.py` maps `id -> file` and keeps the last match alphabetically, so a `DEC-XXXX.redacted.md` can shadow the real decision. Tamped down with `.gitignore` entries in the decisions store, not fixed in the scanner. | low | Lives in the memory/decisions tooling, not in this repo: the fix is to ignore `*.redacted.*` when scanning decisions. |

### Documentation and polish

- **The docs' numbers are not checked by any gate**, which is how "8 MB" and "2 MB" coexisted in
  two files. A small test that reads the constants out of `anon.py` / `web/server.py` /
  `anon-guard.ts` and asserts they still appear in the docs would catch the next drift. Not
  implemented: worth it only if the numbers keep moving.
- The guard's own file (`~/.pi/agent/extensions/anon-guard.ts`) is still not carried by this
  repository, so its history is invisible here (see feature 18).
- A screenshot of the UI in the README (needs an asset that is safe to publish). A working
  capture command on this machine — note that `--dump-dom` hangs here, `--screenshot` does not:

  ```bash
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu \
    --user-data-dir=/tmp/prof --hide-scrollbars --force-device-scale-factor=2 \
    --window-size=1100,900 --virtual-time-budget=3000 \
    --screenshot=/tmp/ui.png http://127.0.0.1:1407/
  ```
- `docs/DESIGN.md` is getting long; it now covers both engine and UI and could be split.
- The CI job installs the converter and skips its tests if that fails — the skip should be visible
  in the run summary rather than only in the log.

### Features worth considering

| # | Feature | Why |
|---|---|---|
| 12 | **`/deanon` command in Pi**, symmetric to `/anon`. | The skill documents the CLI; a command would match the existing gesture. |
| 13 | **Per-client profiles** (`profiles/`, one dictionary each) and a selector. | One growing `entities.txt` becomes unwieldy; today the UI only edits the global one. |
| 14 | **The complete ISTAT municipality list.** The shipped catalog is a 50-city starter. | Must come from the published dataset — the tool must never invent the missing names. |
| 15 | **Batch mode**: anonymize a directory in one command. | Fits the "client folder" workflow. |
| 16 | **`--dry-run`** listing what *would* be redacted, per type, writing nothing. | `--check` is close but has no preview. |
| 17 | **Optional local-model detector** (DESIGN §8): a localhost endpoint that *suggests* candidates which a human approves. | The structural answer to contextual references. The engine stays the only writer. |
| 18 | **Port the `anon` skill and `anon-guard` extension to `pi-workbench`** — or vendor the guard into this repository. | The portable workbench does not carry them yet, and the guard's own history is not versioned here either. |
| 19 | **A phone-prefix catalog.** Deliberately not shipped. | It would compete with the phone rule and fragment numbers: worse than not having it. Revisit only with a real use case. |

### Deeper dives

- **False-positive sweep over a real corpus**: the shipped defaults are now measured against a
  synthetic address corpus, but the rest (hostnames, KEY, near-miss thresholds) is still chosen by
  reasoning. `scripts/bench-check.py` gives the harness for timing; the corpus is still missing.
- **Tag collision probability**: verify empirically over N maps that 6 hex digits is enough, and
  decide whether the tag should be derived from the map id instead of random.
- **Converter fidelity**: how much does the `.docx → Markdown` step lose (tables, headers, tracked
  changes, metadata)? Today the answer is "unknown", and it bounds everything downstream.
- **Guard throughput on other machines**: 12 MB / 20 s was sized on this Mac. A slower machine
  (or a 1000-entry dictionary) shrinks the margin; the cap should eventually be derived at
  runtime from a measured sample instead of being a constant.

## Verified today (not open)

- The new guard, end to end, in a fresh Pi process: a clean file is readable, a sensitive file is
  blocked with its type summary, a 14.4 MB file is blocked with the new cap.
- The container: image builds through `--require-hashes`, the converter works inside it, the docx
  path is redacted end to end, the port is published on loopback only, the `slim` profile exists —
  and the slim image honestly reports `converter: false`.
- `deanon` cannot restore a document with another run's map (CLI and browser DOM).
- The engine suite (74 tests), the web suite (15 tests) and the UI load check are green.
