# Security policy

## Reporting

Open a private security advisory on this repository (or contact the maintainer directly). Please
do not open a public issue for a vulnerability.

## Threat model — what this tool is and is not

anon-tool is a **local** tool. Its whole security value is a boundary, and the boundary is stated
in `docs/DESIGN.md` §7:

- The engine (`anon.py`, `deanon.py`) is deterministic and stdlib-only: **no network capability**,
  no LLM, no telemetry. Anonymization that required a model would first have to hand the model the
  data it is supposed to protect.
- The web UI has **no authentication**. That is acceptable *only* because it binds `127.0.0.1`;
  the container publishes `127.0.0.1:1407:1407`, never `1407:1407`. Exposing the port to a network
  exposes your documents and your entity dictionary — that is a configuration decision, not a bug,
  and it is why the server refuses a non-loopback bind unless `--allow-lan` is passed explicitly.
- Requests must carry the correct `Host` header and a per-run token delivered through a CSP nonce;
  a foreign `Origin` is refused. No client-supplied filesystem path is ever used: uploads land in
  a per-request temp directory under generated names and are deleted afterwards.

## In scope

- any way to make the tool send document content, entity names or map values over the network;
- any way for a page from another origin to drive the API, read a response, or learn the token;
- any path that lets a request read or write outside the sandbox, or reach a subprocess argument
  in a dangerous position;
- any case where the tool reports success while leaving sensitive content in place, or restores
  values that do not belong to that document;
- any way the engine silently fails open without a visible indicator.

## Out of scope (declared limits, see `docs/DESIGN.md` §9)

- **contextual references** ("the client from Brescia") — redaction is deterministic pattern
  matching plus a curated dictionary; a human read of the redacted document is still required;
- **images/screenshots** — pixels are not scannable, so a screenshot of a client document passes;
- **`bash`-style reads** in the Pi guard are outside its default perimeter (`--anon-guard=all`
  extends it to shell output);
- a document whose **only** placeholder happens to exist in a different run's map is now
  impossible to restore (the per-map tag makes it fail loudly), but the tag is part of the
  placeholder: altering it in the document makes restoration fail, by design.

## Supported versions

Pre-1.0: only the current `main` is supported.
