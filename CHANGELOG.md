# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The product version is single-sourced: it lives in `anon.py` (`VERSION`), and `deanon.py`, the web
UI and `--version` all report it. `tests/test_anon.py::CliTest::test_product_version_is_single_sourced`
enforces that they agree.

## [Unreleased]

### Added

- The interface speaks Italian and English, with Italian as the default: a header selector, the
  choice kept in `localStorage`, the Italian text in `index.html` as source and fallback, and
  `web/i18n.js` holding the English keyed by selector. A test fails when a key no longer matches an
  element. Server messages and tooltips are still Italian (OPEN-ISSUES 38).
- `docker-compose.model.yml` + `scripts/fetch-suggest-model.sh`: the local suggestion model
  (Qwen2.5-3B-Instruct Q4_K_M, verified by size and sha256) as a sidecar that SHARES the UI's
  network namespace, so it binds `127.0.0.1:8080` and the seam's loopback-only rule holds
  unchanged. `make model`, `make bench-model`.
- `scripts/bench-suggest.py`: latency and answer quality of a loopback model through the seam.

- **A vendor catalog for infrastructure reports: `catalogs/vendors.txt`** (164 entries,
  `@type FORNITORE`) — hardware and software manufacturers, one line per COMPANY, ticked on demand
  like any other catalog (`--catalogs vendors`, or the checkbox in the UI). The list is split into
  two blocks and that split is the design, with ONE rule: `@match case-sensitive` for the names whose
  lowercase is also an ordinary word or unit (`Dell`/`dell'aria`, `Canon`/`canon`, `Axis`/`axis`,
  `Acer` the maple genus, `Apple`/`apple`, `Juniper`, `Brother`, `Snowflake`, `Slack`, `Elastic`,
  `Tenable`, `Xerox`, `Adobe` the building material, `Google`/`to google`, `Oracle`,
  `Intel`/"threat intel", `Cisco` and `Barracuda` the fish, `Siemens` the SI unit, `Okta` the
  cloud-cover unit, `Gigabyte` the unit, `HP` horsepower, `SAP` tree sap, `Arista` the Italian roast,
  `Moxa`, `Sage`, `Zebra`, `Ruckus`, `Red Hat`, `New Relic`, `Open Text`) and `@match insensitive`
  for the rest, so a lowercase spelling in a filename, a hostname or a spreadsheet matches too
  (`un server ibm`). The rule is measurable — `grep -ix "<name>" /usr/share/dict/words` — which is
  how the wrong-block entries below were found, and it is enforced by the tests below.
  The list was sized up before shipping with `scripts/fp-sweep.py` (which now takes `--entities`, so
  a candidate list can be measured the same way): on the first candidate (54 clean source/doc files,
  measured before this file shipped) `Docker` alone accounted for 37 of the 49 hits — three quarters
  of the noise from one word — so it is out, together with `Kubernetes` and `Ubuntu` (the word is the
  TECHNOLOGY), and `LG`, `F5`, `MSI`, `AVG`, `Sharp`, `Crucial`, `Canonical`, `Meta`, `Zoom` (the
  surface is a common acronym or an ordinary word; the company is listed as `F5 Networks`). The
  declared gaps are in the file's header: product/model names, the lowercase spelling of a
  case-sensitive name, and the sentence-initial Italian elision (`Dell'azienda risulta…` IS redacted
  — an apostrophe is not a word character, and the format has no per-entry exclusion).
- **Fifteen names that were in the wrong block, found by the two adversarial reviews — and the false
  rationale that put four of them there.** The first review found `Gigabyte` (the unit), `Red Hat`,
  `Avast`, `Veritas`, `Trend Micro` and `Western Digital` in the case-insensitive block, where
  `un gigabyte di memoria` and `she wore a red hat` would have been redacted — the exact failure the
  two-block split exists to prevent — plus `Aruba`/`Kingston`/`Eaton` needlessly case-sensitive. The
  review of that fix then found the opposite error twice over: `Cisco` (a fish, and an enum-listed
  word) had been moved INTO the insensitive block by the fix itself, `Okta` (a cloud-cover unit) was
  still there, and `IBM`/`AMD`/`HPE`/`APC` were sitting in the case-sensitive one because the header
  called them "identifiers in source code" — a rationale that does not survive a word list (`ibm` is
  neither a word nor a variable name), and which cost a real MISS: `un server ibm` was not redacted.
  All fifteen moved, the rule is now one line instead of a judgement call, and six wrong-block
  entries plus the under-redaction direction are pinned by
  `VendorsCatalogTest::test_no_second_block_name_is_an_ordinary_word` and
  `::test_a_second_block_name_matches_however_it_is_capitalized`. That pinning covered the shapes
  that had been found and nothing else; the GATE for a name added later is the bullet after this
  one.
  The reviews also found a tautological assertion in the size test (it compared `entity_count` with
  itself; it now reads the declared LINES, which is what catches a duplicated or silently dropped
  entry) and a vacuous one (`a4`).
- **The two-block rule is now a GATE, not a convention** (`docs/OPEN-ISSUES.md` #37, closed).
  `VendorsCatalogTest` reads the two blocks out of the FILE and enforces both directions: every
  case-sensitive name must record the ordinary word it collides with (`WORD_COLLISION`), and no
  block-2 name may be one of those words or a word of the OS dictionary — the sweep that catches a
  name nobody thought about, with a VISIBLE skip when there is no dictionary to sweep (a silent
  skip reads as "everything ran"). Both halves are needed: the OS list has no `gigabyte`, `google`,
  `veritas`, `siemens`, `okta`, `hp`, `intel`, `xerox` or `arista` either. Proven to bite by
  mutation, four ways: a word moved into block 2, a recorded word deleted, an unjustified
  case-sensitive name added, and a dictionary word (`Onyx`) appended to block 2 — each fails.
- **The list grew from 116 to 164 entries**, chosen the same way and checked by the gate above:
  the networking and security vendors an infrastructure report names (`Palo Alto Networks`,
  `Extreme Networks`, `A10 Networks`, `ZTE`, `SolarWinds`, `Cloudflare`, `Akamai`, `Fastly`,
  `Netskope`, `Darktrace`, `Trellix`, `Forcepoint`, `Mimecast`, `Rubrik`, `Avira`), the compute and
  imaging ones (`MediaTek`, `ASRock`, `Konica Minolta`, `Datalogic`, `Zebra`), the OT/industrial
  ones (`Moxa`, `Advantech`, `Belden`, `Riello`), the software names (`JetBrains`, `Databricks`,
  `Cloudera`, `Teradata`, `Autodesk`, `Open Text`, `Dynatrace`, `Paessler`, `Dropbox`,
  `Zoho`, `HubSpot`, `Zendesk`, `Schneider Electric`) and the Italian software houses (`Zucchetti`,
  `TeamSystem`, `Dedagroup`, `GPI`, `Almaviva`, `Maggioli`). Eight of them are words and went to
  block 1 with the word recorded (`Ruckus`, `Arista`, `Oki`, `Zebra`, `Moxa`, `Sage`, `New Relic`,
  `Open Text`). The rule survived the additions unchanged, and one thing it made explicit is why a
  camel-case compound can stay in block 2 while `Open Text` cannot: the separator rule applies only
  BETWEEN the words of a multi-word entry, so `SolarWinds` does not match "solar winds" while
  `Open Text` does match "open text" — verified against the engine, not assumed. Cost checked at
  the new size with `scripts/bench-check.py --entities 164 --mb 5` (the figure belongs to the
  machine: the command is documented, not the number).
- **`VendorsCatalogTest`** (15 tests) pins the properties a data file cannot otherwise fail on:
  case-sensitivity on the homographs, whole-word anchoring (`Dell` must not match `DellOrto`), the
  case-insensitive block, the exact spelling kept in the map (`SonicWall` + `sonicwall` = two
  placeholders, declared), inertness until the catalog is selected, and — the one that would have
  destroyed a document — that the container path never rewrites an XML ATTRIBUTE, so
  `urn:schemas-microsoft-com:vml` survives and the package stays valid.
- **`scripts/fp-sweep.py` takes `--entities`**, so a candidate dictionary or catalog can be sized up
  before it ships. The catalog files are left out of the corpus only in that mode: excluding them
  from the DEFAULT sweep would have hidden 34 real pattern matches (a host in a catalog comment, a
  phone number in its prose), which is precisely what the tool exists to report. A `--entities` path
  that does not exist is now a refusal (exit 2) instead of an empty dictionary reported as "clean".
- **`scripts/check-doc-numbers.py` now binds the size of the shipped catalogs** to the files
  themselves, in `catalogs/README.md` and in the `vendors.txt` header (the number in this changelog
  is history, and out of scope like every other one here).

- **A second shipped catalog: `catalogs/products.txt`** (130 entries, `@type PRODOTTO`) — the
  product and platform names an infrastructure report names (`FortiGate`, `PowerEdge`, `Catalyst`,
  `vSphere`, `Windows`, `Docker`, `Kubernetes`), as opposed to the companies that make them, which
  stay in `vendors.txt`: a name lives in ONE of the two, and a test fails if it appears in both,
  because the same surface would otherwise get two types depending on which list was ticked first.
  The block rule is the same and is enforced by the same gate, and here it works harder — a product
  name is very often an ordinary English word, so block 1 is long (`Word`, `Excel`, `Access`,
  `Teams`, `Windows`, `Exchange`, `Outlook`, `Catalyst`, `Nexus`, `Umbrella`, `Firepower`,
  `Firebox`, `Falcon`, `Defender`, `Horizon`, `Android`, `Chrome`, `Safari`, `Thunderbird`,
  `Azure`, `Docker`, `Helm`, `Rancher`, `Tomcat`, `Apache`, `Zoom`). The gate caught one during the
  writing: `Thunderbird` had been left in block 2 and the dictionary sweep refused it — the bird is
  a word. Model and release numbers (`R740`, `DL380`, `NSa 2700`, `Windows 11`) are deliberately out:
  they change every quarter and arrive as hostnames. **The shared gate is now a reusable class**
  (`CatalogBlocksTest`), so the vendor and product lists cannot drift apart in how they are checked,
  and `scripts/check-doc-numbers.py` binds the size of both and of the PAIR (294 entries — the way
  the two lists are meant to be used). The pair's throughput is `scripts/bench-check.py --entities
  294 --mb 5`: the number belongs to the machine, so the command is what is documented, not a figure
  that ages. Declared and NOT closed: product/model numbers, and the sentence-initial elision.
- **The local-model seam, scaffolded: `suggest.py`** (`docs/DESIGN.md` §7, `docs/OPEN-ISSUES.md`
  #17). A local model plugs in as a **detector**, never as the anonymizer, and the seam is a CLIENT
  of the engine — `anon.py` does not know it exists. Three properties are enforced, each with its
  own test: **loopback only** (the endpoint must be `localhost`, `127.0.0.0/8` or `::1`, refused
  before any request — a document must not leave the machine, and a "helpful" LAN endpoint would
  break that silently), **fail-closed** (a timeout, a non-2xx or unparseable output is exit 2, never
  an empty "nothing found": those are different facts), and **writes nothing** (no redacted copy, no
  map, no placeholder — proposals are exit 4, to review; applying one stays `anon.py`'s job). The
  model's answer is located in the text BY US, so a hallucinated value costs nothing, and the
  deterministic `detect()` output is shown alongside each proposal so a duplicate is visible.
  `tests/test_suggest.py` (17 tests) drives a real loopback HTTP server for the happy path and
  asserts the refusals, the failure modes, the truncation being declared, and that the seam leaves
  the filesystem untouched. `OfflineContractTest` gained the declared exception: `suggest.py` is the
  ONE file allowed to import a network stack, the engine never imports it, and both halves are
  tested so a second module cannot quietly acquire the capability.
- **The products catalog and the seam are wired into the gates**: `make test` and CI run
  `tests/test_suggest.py`, and the CI run summary names it.

### Changed

- **The redacted document is streamed instead of base64-encoded into the JSON response.** It is
  published under `~/.anon/downloads/` (0600, pruned after an hour) and fetched from
  `GET /api/download/<id>` in 64 KB blocks, so the server holds one block at a time instead of
  building ~1.33x the file as a string on top of the file, the Markdown and the decoded copies.
- **The index over the visible text no longer costs tens of bytes per character**: the text is built
  from chunks and joined once, the offsets live in an `array('i')`. `python3 scripts/bench-index.py`
  on a 16.2 MB XML part (12.6 MB of visible text): peak RSS **544 MB -> 100 MB** (about 43 -> 8 bytes
  per visible character, whole-process RSS, not the index alone), at ~23% more time. The script runs
  both implementations, each in its own process, so the number can be re-measured instead of
  remembered.

### Fixed

- **The boundary rule was still wrong, and adversarial review found the DESTRUCTIVE case it hid.** The
  check stopped at the first difference between the fragments' element paths, so a fragment inside a
  text box nested in a run (`w:drawing`, and the legacy VML `w:txbxContent`) looked "safe" and the text
  box's text was DELETED. Two more holes in the same decision: only the first and last fragments were
  checked (a value whose middle landed in another container passed), and `m:oMathPara`/`m:oMath` counted
  as inline (two equations = one text). The decision is now a SIGNATURE — every ancestor that is not an
  inline element — compared for EVERY fragment, verified with the review's own reproductions in both
  directions and against the legitimate splits that must keep working.
- **Three more defects of the same class, found by hunting for it.** (1) `deanon`'s repair had the
  mirror of the destructive rewrite: across a container boundary it moved the value into the first
  fragment and deleted the second paragraph's text — and its test asserted that behaviour while
  claiming to test a run split. The boundary rule now runs in both directions from the same code, and
  the test uses a genuine run split. (2) The inline element set was incomplete (tracked deletions via
  `w:delText`, math runs, field characters, legacy markers, text boxes), so a value split there
  refused an ordinary document — though on review only the MATH case is a real behaviour change: in
  the other seven the difference was already a `w:r`. The corpus test protects against the opposite
  mistake, refusing a legitimate split. (3) A part
  named as text that could not be decoded was SKIPPED — now refused in the redaction and reported as
  `unreadable_parts` in the restore, which can no longer answer `complete`.

- **A refusal on ordinary documents, corrected twice the same day — the mechanism, not the list.**
  The rule that refuses a match spanning a structural boundary classified TAGS one by one, so the
  run-properties block Word writes on every run (`<w:rFonts/>`, `<w:sz/>`, `<w:spacing/>`) looked
  like a boundary and a real footer was refused, leaving the UI with the Markdown alone. It now
  compares the CONTAINER of the two fragments: the walker records the element path (names AND
  instance ids) each fragment lives in, and joining is safe when they diverge only on inline
  elements — a run, a run property, a tab, a break, a bookmark, a hyperlink, a content control.
  `</w:p><w:p>` and `</dc:title><dc:creator>` still put the halves in different containers and are
  refused, naming the element. Instance ids are load-bearing: by name alone two runs of one
  paragraph and two paragraphs are indistinguishable, and the tests fail if the comparison
  downgrades to names or stops refusing. Verified end-to-end on the real footer shape (run
  properties between the fragments) and on tab/break/bookmark/content-control splits: all come back
  as a REDACTED DOCX; the two-paragraph case is refused with `w:p` named.
- **`--batch` no longer follows a symlink out of the tree it was pointed at.** `is_file()` follows
  links, so a link inside the folder could pull in a file nobody put in scope — including a map from
  the private store, where the real values live. A candidate whose target is inside `~/.anon` or
  leaves the scan root is skipped, with its reason in the report; a test with both kinds of link
  fails against the old code (`docs/OPEN-ISSUES.md` #36).
- Adversarial review of the container pass found four real defects, all fixed here and each covered
  by a test that fails against the old code: a **deadlock** (the PDF fallback re-took `TAG_LOCK`,
  which is not reentrant: one PDF hung the request and every later one), a **BOM-less UTF-16/32
  part** being treated as binary (never scanned, never verified, reported as "0 leftovers"), **no
  bound on decompression** (a 61 KB file could ask for gigabytes, reachable from `--check`, which is
  the guard's `read` path), and a **match spanning a structural boundary** destroying the other
  element's text. Smaller ones: the map is no longer written before the conversion that can fail,
  and the response withholds the document beyond the upload cap instead of building it in memory.

### Added

- **The UI offers the document, not only its text**: uploading a `.docx`/`.xlsx`/`.pptx`/`.odt`
  returns `verbale.redacted.docx` to download — same type, same layout — plus the Markdown for the
  model. ONE redaction produces both artifacts (the Markdown is derived from the already redacted
  file), so they share tag and map and cannot disagree; a PDF or an unreadable package falls back to
  the Markdown alone and says why.
- **`--audit` answers on a container too** (verdict, surviving values, and the placeholder count inside the parts)
- **`--check` on a container names the findings** while KEEPING `unscannable: true` — that field
  describes what the Pi `read` tool would do with the file, and the guard's auto-remediation keys on
  it. The findings name types and positions, never the values.
- **xlsx / odt / pptx are now proven**, not assumed: a fixture per format asserts the value is gone
  from the part the format keeps its text in (`xl/sharedStrings.xml`, `content.xml`,
  `ppt/slides/slide1.xml`) and that the restore puts it back there. The tests were checked against a
  mutated engine (`decode_part` neutered): 5 of them fail, so they bite.
- **`anon.py verbale.docx` → `verbale.redacted.docx`**: an office container is redacted IN PLACE,
  part by part, so the document comes back as the same kind of file — layout and styles intact —
  instead of as Markdown. It also covers what the Markdown path loses (measured: 6 features of 17):
  headers and footers, comments, footnotes and the document properties (the AUTHOR), so a client
  name in a letterhead is no longer left in the clear. The written file is re-read and re-scanned
  before anything is delivered; if a value survives, the output is deleted and the run fails.
  `--batch` picks containers up too, `--dry-run` shows what would change. DEC-0015 supersedes the
  blanket refusal of DEC-0011 for the formats we can rewrite; legacy `.doc/.xls/.ppt`, PDF and
  images are still refused, as is any file that claims to be a container but is not a readable ZIP.
### Fixed

- **The Anonimizza button never armed for a DROPPED document** (only the file picker worked): the
  drop stored nothing, and the button read the file INPUT's `files`, which a drop does not
  populate. Both paths now go through one `acceptAnonFile`, and a dropped document reports its name
  and size. Covered by new interaction checks in `tests/ui_load_check.mjs`, which now dispatches
  real drop events instead of only proving that the script parses.
- A file the server would refuse is now refused in the browser, before spending the transfer, with
  the limit named in the message (the cap comes from `/api/state`, so the page cannot drift from it).
- `--batch --check` reported a folder containing only `.docx` as clean (fail-open): unscannable
  files are findings now, exactly like the single-file check.
- `--stdout` had stopped writing the map (regression from the `write_run` refactor): the pipeline
  could no longer be reversed. Restored, with a test.
- `--batch --out DIR` flattened every output to its basename, so `a/nota.txt` and `b/nota.txt`
  overwrote each other; the relative path is preserved. An `--out` equal to (or a parent of) the
  scanned folder is refused instead of producing a silent no-op.
- `_is_own_output` matched `.redacted.` anywhere, skipping real sources such as
  `note.redacted.draft.txt`, and missed `X.REDACTED.MD`: it is anchored to the final extension and
  case-insensitive now.

### Added

- `anon.py --dry-run`: reports what WOULD be redacted, per type, and where it would be written,
  creating no file and allocating no tag. `--check` answers "is this sensitive?"; this answers the
  question that comes first.
- `anon.py DIR --batch`: anonymizes a folder, writing `*.redacted.*` next to each source and never
  over it. It skips what it cannot do safely — binary/unscannable files (never copied under a
  `redacted` name), anything over 12 MB (rather than half-processing it), and its own
  `*.redacted.*`/`*.map.json` outputs, so a second run cannot nest placeholders — and refuses a
  directory inside `~/.anon` outright. `--batch --check` is the folder-shaped gate (exit 1 if any
  file is sensitive); `--batch --out DIR` keeps the source folder clean.
- A progress bar on the Anonimizza tab: the upload percentage (XHR reports what `fetch` cannot),
  then an elapsed-time sweep while the server converts and anonymizes — kept visible for at least
  700 ms, so a fast run does not merely flicker.
- `ANON_MAX_UPLOAD_BYTES` (default 160 MB, plumbed through `docker-compose.yml`): the upload cap is
  the operator's decision. Raising it does not raise the conversion timeout, nor the 12 MB the Pi
  guard is willing to read.
- `scripts/convert-fidelity.py` measures what `.docx -> Markdown` loses, proving each feature is
  in the fixture before looking for it in the Markdown: **11 of 17** features survive. Headers,
  footers, comments, tracked deletions and the core properties (title, **author**) are not carried
  — so the engine never redacts them and they stay in the original `.docx` and in any PDF exported
  from it. The workflow consequence is in the `anon` skill.
- `docs/brand/ui-anonymize.png` and the README's Web UI section: a real capture of the UI, from a
  server running on a temporary `ANON_HOME` with synthetic data (never the container's real one).
- CI: the converter-install outcome is now an output, a `::warning` and a line in the run summary
  ("NOT installed — the docx/pdf tests were skipped"), instead of a skip visible only in the log.
  CI also runs the doc-number gate.

### Changed

- `docs/DESIGN.md` states the engine only: the local web app's perimeter (bind address, token,
  `Host`/`Origin`, the caps, the container hardening) moved into `SECURITY.md`, where the threat
  model already pointed at it. One document per audience, and the perimeter is no longer written
  in two places.

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
