# Third-party notices

anon-tool itself is MIT (see `LICENSE`). It depends on the following third-party work; each is
used as documented and none of them is bundled in this repository.

## Runtime (the container installs it, pinned)

- **firecrawl-anydoc** — <https://github.com/firecrawl/anydoc> — MIT — pinned to `0.2.3` in
  `Dockerfile` and in `convert.py` (`PIN`). Converts documents (doc, docx, odt, rtf, epub, pdf,
  presentations, spreadsheets, csv) to GitHub-Flavored Markdown.
  Provenance note: the wheel ships a compiled Rust extension (`_anydoc.abi3.so`), so its source
  is not in the wheel. It was inspected at the dependency level — the conversion venv contains no
  HTTP library, and the compiled module references no `socket`, `getaddrinfo` or TCP/TLS client
  symbols; the only URLs in it are OOXML namespace strings. It is treated as a parsing library,
  run locally, and never trusted with credentials.

## Development / tests only (not part of the runtime image)

- **pandoc** — <https://pandoc.org> — GPL-2.0-or-later — used by the test suite to build `.docx`
  fixtures and to read results back. It is invoked as an external process, never linked.
- **Python standard library** — PSF-2.0 — everything the engine uses. There is no pip install for
  the engine itself.

## Design references (no code copied)

- The engine is written from scratch against published algorithms: the *codice fiscale*
  check-character tables, the partita IVA control digit (DPR 633/1972), ISO 13616 (IBAN mod-97).
  The test suite pins them to known-good examples so a wrong implementation fails loudly.
