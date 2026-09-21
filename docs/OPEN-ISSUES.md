# Open issues — for the next pass

Everything below is known, deliberate, or measured-but-unresolved. Nothing here is a surprise: it
is the list of what a second pass should pick up, roughly in the order I would tackle it.

## Bugs and correctness

| # | Issue | Severity | Note |
|---|---|---|---|
| 1 | **The Pi guard scans at most 2 MB per file, while the UI accepts 160 MB.** A large redacted Markdown is *blocked* when the agent tries to read it. | medium | Fail-closed, so nothing leaks — but the pipeline cannot consume its own output past 2 MB. Either raise the cap with a measured throughput, or stream the scan. |
| 2 | **`@stem` can over-match on short stems.** Declaring `@stem on` for a 3–4 character name would redact unrelated suffixes. | medium | Emit a warning when a stem entry is shorter than ~5 characters, or refuse it. |
| 3 | **Address detection is conservative and noisy at the edges.** `via Roma 12` works; `corso di 3 giorni` can be caught, and `Via G. Verdi 3/A` needs checking. | medium | Needs a false-positive sweep over real documents before it is trusted. |
| 4 | **Word splitting a placeholder across two runs is detected, not repaired.** `deanon` exits 3 and tells you to regenerate from Markdown. | low | A run-merge repair is possible, but merging `<w:t>` nodes can move formatting; the workaround is honest and safe, so this stays open on purpose. |
| 5 | **The audit's near-miss search is bounded** (400 words, 200 entities). Past that it silently truncates. | low | The API reports `candidates_capped`; the UI does not surface it yet. |
| 6 | **The reveal view keeps the real values in the DOM** with no timeout or relock. | low | Local-only page, but a "hide again" action would be cheap. |

## Robustness and hardening

| # | Issue | Note |
|---|---|---|
| 7 | **No rate limiting on the local API.** Loopback-only, but a runaway script could saturate it. | Low risk; a simple token-bucket would close it. |
| 8 | **The converter caps its output *after* buffering it.** A zip bomb still inflates memory before the 160 MB check. | Needs an incremental read with an early abort. |
| 9 | **The Docker build needs network and pins anydoc by version, not by hash.** | Add `--require-hashes` or vendor the wheel. |
| 10 | **The maps directory grows unbounded.** No rotation or cleanup policy for `~/.anon/maps`. | Add `anon.py --prune-maps <days>` and mention it in the skill. |
| 11 | **The slim image (`WITH_CONVERTER=0`) exists only as a build arg**, not wired into compose. | A compose profile or a second service would make it discoverable. |

## Features worth considering

| # | Feature | Why |
|---|---|---|
| 12 | **`/deanon` command in Pi**, symmetric to `/anon`. | The skill documents the CLI; a command would match the existing gesture. |
| 13 | **Per-client profiles** (`profiles/`, one dictionary each) and a selector. | One growing `entities.txt` becomes unwieldy; today the UI only edits the global one. |
| 14 | **The complete ISTAT municipality list.** The shipped catalog is a 50-city starter. | Must come from the published dataset — the tool must never invent the missing names. |
| 15 | **Batch mode**: anonymize a directory in one command. | Fits the "client folder" workflow. |
| 16 | **`--dry-run`** listing what *would* be redacted, per type, writing nothing. | `--check` is close but has no preview. |
| 17 | **Optional local-model detector** (DESIGN §8): a localhost endpoint that *suggests* candidates which a human approves. | The structural answer to contextual references. The engine stays the only writer. |
| 18 | **Port the `anon` skill and `anon-guard` extension to `pi-workbench`.** | The portable workbench does not carry them yet, so the integration is only complete on this machine. |
| 19 | **A phone-prefix catalog.** Deliberately not shipped. | It would compete with the phone rule and fragment numbers: worse than not having it. Revisit only with a real use case. |

## Deeper dives

- **Measure the guard's throughput** against a realistic dictionary (200+ entries) to set the scan
  cap from data instead of an estimate — this is the prerequisite for issue 1.
- **False-positive sweep**: run the engine over a corpus of real documents and measure, per pattern
  and per catalog, how much is redacted and what is wrongly redacted. The shipped defaults were
  chosen by reasoning, not by measurement on a corpus.
- **Tag collision probability**: verify empirically over N maps that 6 hex digits is enough, and
  decide whether the tag should be derived from the map id instead of random.
- **Converter fidelity**: how much does the `.docx → Markdown` step lose (tables, headers, tracked
  changes, metadata)? Today the answer is "unknown", and it bounds everything downstream.

## Documentation and polish

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

## Found while closing (fixed, but the cause is worth knowing)

- A tool run over a *decisions* directory can leave `DEC-XXXX.redacted.md` next to `DEC-XXXX.md`.
  `verify.py` maps `id -> file` and keeps the last match alphabetically, so such a file can shadow
  the real decision during verification. Prevented with `.gitignore` entries in the decisions store;
  the structural fix (make the decision scan ignore `*.redacted.*`) is still **open**.

## Verified today (not open)

- The Pi skill integration works end to end: `convert → anon → --check → audit → deanon`, with the
  catalog context gate firing correctly on `Sede di Brescia`.
- The container serves the current front-end (an earlier build was stale — rebuilt and rechecked).
- A document can no longer be restored with another run's map: verified at the CLI and in the
  browser DOM.
