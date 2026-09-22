# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The product version is single-sourced: it lives in `anon.py` (`VERSION`), and `deanon.py`, the web
UI and `--version` all report it. `tests/test_anon.py::CliTest::test_product_version_is_single_sourced`
enforces that they agree.

## [Unreleased]

## [1.7.0] - 2026-09-22

### Added

- `@context off` in a dictionary/catalog: a directive applies to the entries that follow it and a
  later one replaces it, so `@context` opens a block and `@context off` closes it — only the exact
  token `off`, because `@context no` and `@context 0` are legitimate regexes and must keep working.
  Before this, narrowing a context meant reordering the file. The ordering rule is now stated in
  `catalogs/README.md`, `docs/DESIGN.md` and the parser's own docstring.
- The per-map placeholder tag is allocated against the tags **already in use** by the maps on disk,
  in both the default directory and the destination of `--map`: a guarantee is only as wide as the
  scan behind it. `scripts/tag-collision.py` measured what the code comment used to assert: with
  6 hex alone the collision probability is ~3% at 1 000 maps, ~53% at 5 000. `SECURITY.md`'s "a
  collision stays negligible" was the same guess in prose, and is corrected. Two runs allocating in
  the same instant, and maps under a different `ANON_HOME`, are declared residual gaps.
- `scripts/check-doc-numbers.py`, wired into `make test`: the numbers in `README.md`,
  `docs/DESIGN.md`, `SECURITY.md` and the UI copy are read from the constants (version, tag width,
  every placeholder example, the collision percentages, upload cap, converter cap and timeout, rate
  limit, the near-miss bounds, the guard's cap). Each claim is anchored to the sentence that reports
  it — a bare number would be satisfied by the same number for a different constant, which is how two
  160 MB caps in one file slipped past a substring check. On its first run it caught five
  placeholder examples written with four hex digits while the engine writes six.
- `OfflineContractTest`: an AST **canary** — no engine script (`anon.py`, `deanon.py`,
  `convert.py`) may gain a *static* import of a network stack, and the two document paths may not
  import `subprocess`. It is a canary, not a proof: a dynamic or transitive import slips past, and
  `convert.py --install` reaches the network through pip on purpose. The test says so itself, so
  the gate is not read as a stronger guarantee than it is.

## [1.6.0] - 2026-09-22

### Added

- The curated dictionary is split by kind, all optional and read together: `~/.anon/entities.txt`
  (the generic fallback), `people.txt` (`@type PERSONA`; a name, or a name plus an email alias) and
  `clients.txt` (`@type AZIENDA`; a company name and its sites). `--entities PATH` is now
  repeatable and overrides them.
- The web UI's **Dizionario** tab selects and edits the three files through the engine
  (`/api/entities?file=entities|people|clients`); an unknown name is a `400`, never a silent
  fallback. `/api/state` reports `entities_paths`.

## [1.5.0] - 2026-09-22

### Added

- `scripts/fp-sweep.py`: measures the engine's false positives over a real, known-clean corpus,
  aggregated per rule type. Deterministic (a counter over `detect()` matches), never an LLM.

### Fixed

- **The KEY rule redacted ordinary code** (item #20). A right-hand side that is a CALL
  (`token = re.compile(...)`, `secret = scanForSecrets(x)`, `unicodedata.normalize(...)`) is now
  rejected by the pattern, so a code reference is no longer mistaken for a literal secret. A DOTTED
  value is deliberately KEPT (`password = my_secret.phrase`, `admin.secret`): it can be a real
  password, and a missed secret is worse than a false positive.

### Measured

- On the clean sweep corpus (48 files: this repo, the Pi extensions, the installed Pi package), the
  KEY rule went from **11 matches to 5**; the 5 are two documentation comments in `anon.py` and
  three code references it deliberately keeps (`variant.first_token`, `window.ANON_TOKEN`,
  `__ANON_TOKEN__`). Private configs (real IPs/passwords) are excluded — a match there is a true
  positive. See `docs/OPEN-ISSUES.md`, item #20.

## [1.4.0] - 2026-09-22

### Added

- `anon.py --allow-glob GLOB` (repeatable): extra path globs treated as un-sensitive for a single
  run, same syntax as `~/.anon/allow.txt`. It is what the Pi guard uses for a session-only
  allowlist, without writing a temp file. The allowlist is still evaluated only by the engine.
- Pi guard (`anon-guard.ts`): `--anon-guard-allow='/a/*,/b/*'` (or `PI_ANON_GUARD_ALLOW`) adds
  session-only path globs on top of `~/.anon/allow.txt`; the `/anon-allow <path>` command appends a
  glob to the file (a directory becomes `/path/*`) after a confirmation.

### Changed

- The Pi guard now treats the Markdown produced from a path declared un-sensitive as un-sensitive
  too (via `allow.txt`, `--anon-guard-allow` or `--allow-glob`), so allowlisting a folder also
  covers the `.docx`/`.pdf` converted from it.
- The example company in the docs, tests and smoke script is now fictional (`Contoso`), so the
  public repository no longer carries a real entity from `entities.txt`.
- `deanon.py` no longer keeps its own version string: it reports `anon.VERSION`.

### Fixed

- The product version was two independent strings (`anon.py` 1.3.0, `deanon.py` 1.2.0) and the
  repository carried no tags or changelog. It is now one value, surfaced in the README and here.

## [1.3.0] - 2026-09-22

Starting point of this changelog. Earlier history is in `git log`.
