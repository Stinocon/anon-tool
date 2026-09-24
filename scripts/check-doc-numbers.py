#!/usr/bin/env python3
"""check-doc-numbers.py — the numbers in the prose must come from the code.

Two files disagreed (the upload cap was "8 MB" in one and 160 MB in the other) for a whole pass,
the map tag was documented as "4 hex" while the code produced 6, and SECURITY.md promised that a
tag collision "stays negligible" — which `scripts/tag-collision.py` measures as ~3% at a thousand
maps. All three were found by hand. This makes the class checkable: every entry below binds a
CONSTANT to the place that reports it.

Scope is deliberate:

  * checked: the documents that state CURRENT behaviour — `README.md`, `docs/DESIGN.md`,
    `SECURITY.md` (the web perimeter and its caps live there) — plus the user-facing copy in
    `web/app.js`;
  * NOT checked: `CHANGELOG.md` and `docs/OPEN-ISSUES.md`, which record HISTORY. A changelog entry
    saying "the cap was 2 MB" is correct about that release and must not be rewritten.

The guard's own constants (12 MB / 20 s) live in the Pi extension (`anon-guard.ts`, shipped by
pi-workbench and installed under `~/.pi/agent/extensions/`), which is a different repository. That
claim is checked when the file can be found, and reported as SKIP when it cannot — visibly, because
a silent skip reads as "everything ran".

    python3 scripts/check-doc-numbers.py
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import anon  # noqa: E402

MB = 1024 * 1024


def load_server_module():
    """`web/server.py` defines its caps at module level and importing it starts nothing."""
    spec = importlib.util.spec_from_file_location("anon_web_server", ROOT / "web" / "server.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def birthday(n: int, space: int = 16 ** 6) -> float:
    """P(at least one collision among n draws) — the same closed form scripts/tag-collision.py uses.

    Duplicated on purpose: the gate must catch a number that DRIFTS in the prose, so it cannot read
    the number from the very report it is policing.
    """
    return 1.0 - math.exp(-n * (n - 1) / (2.0 * space))


def main() -> int:
    failures: list[str] = []
    checked = 0
    skipped: list[str] = []

    def claim(label: str, pattern: str, doc: str) -> None:
        """`pattern` must match in `doc`, ANCHORED to the sentence that reports the constant.

        A bare `"160 MB" in text` would be satisfied by any other 160 MB in the same file — the
        upload cap and the converter cap are both 160 MB, so the sentence has to be part of the
        check, not just the number.
        """
        nonlocal checked
        checked += 1
        if not re.search(pattern, read(doc)):
            failures.append(f"{label}: {doc} has no match for /{pattern}/")

    server = load_server_module()

    # 1. the product version, stated once in the README and owned by anon.py
    claim("version", rf"<strong>v{re.escape(anon.VERSION)}</strong>", "README.md")

    # 2. the tag width, and the examples that show it (they must look like real output)
    claim("tag width (DESIGN)", rf"{anon.TAG_DIGITS} hex digits", "docs/DESIGN.md")
    claim("tag width (SECURITY)", rf"{anon.TAG_DIGITS} hex digits", "SECURITY.md")
    example = re.compile(r"\[[A-Z][A-Z0-9_]*-\d+-([0-9a-f]+)\]")
    for doc in ("docs/DESIGN.md", "SECURITY.md", "README.md"):
        for match in example.finditer(read(doc)):
            checked += 1
            # 6 is what the allocator draws first; 8 is the documented widening when 6 is taken.
            if len(match.group(1)) not in (anon.TAG_DIGITS, 8):
                failures.append(
                    f"placeholder example in {doc}: {match.group(0)} carries {len(match.group(1))} "
                    f"hex digits; the engine writes {anon.TAG_DIGITS} (8 when it has to widen)"
                )

    # 3. the collision numbers the prose quotes, bound to the birthday formula they come from
    claim("collision chance (DESIGN)", rf"~{round(birthday(1000) * 100)}% at 1 000 maps", "docs/DESIGN.md")
    claim("collision chance (SECURITY)", rf"~{round(birthday(1000) * 100)}% at 1 000 maps", "SECURITY.md")
    claim("collision chance at 5 000", rf"~{round(birthday(5000) * 100)}% at 5 000", "docs/DESIGN.md")

    # 4. the web server's caps, each anchored to its own sentence
    megabytes = server.MAX_BODY_BYTES // MB
    claim("upload cap", rf"size limits on uploads \({megabytes} MB[^)]*\)", "SECURITY.md")  # the env var may be named after the number
    claim("converter output cap", rf"default {server.CONVERT_MAX_BYTES // MB} MB\)", "SECURITY.md")
    claim("converter timeout", rf"{server.CONVERT_TIMEOUT_SECONDS} s, `ANON_CONVERT_TIMEOUT`", "SECURITY.md")
    claim("rate limit (README)", rf"default {server.DEFAULT_RATE_LIMIT}/min", "README.md")
    claim("rate limit (SECURITY)", rf"default {server.DEFAULT_RATE_LIMIT}/min", "SECURITY.md")

    # 5. the near-miss bounds, as told to the user. The wording moved into the language table with
    # the bilingual interface, and it exists in BOTH languages: binding only one would let the other
    # go stale. The numbers stay the engine's constants.
    for language, wording in (
        ("it", rf"{anon.NEAR_MISS_WORD_LIMIT} parole / {anon.NEAR_MISS_VOCAB_LIMIT} entità"),
        ("en", rf"{anon.NEAR_MISS_WORD_LIMIT} words / {anon.NEAR_MISS_VOCAB_LIMIT} entities"),
    ):
        claim(f"near-miss bounds ({language})", wording, "web/i18n.js")

    # 6. the locator threshold: the number in DESIGN is the constant that decides which locator runs
    claim("scan word threshold", rf"{anon.SCAN_WORD_SOURCES_MIN} is the middle", "docs/DESIGN.md")

    # 6b. the local model's bounds, as declared to the operator: the window that reaches it and the
    # timeout we wait for an answer. Both are the seam's own constants, and the README states them.
    import suggest  # noqa: E402 - the engine is already importable from ROOT

    window = f"{suggest.DEFAULT_MAX_CHARS:,}".replace(",", " ")
    claim("suggest window (README)", rf"{window} characters", "README.md")
    claim("suggest timeout (README)", rf"{suggest.DEFAULT_TIMEOUT:g} s", "README.md")

    # 7. the counts stated for the shipped catalogs, bound to the files themselves
    catalogs = {name: ROOT / "catalogs" / f"{name}.txt" for name in ("it-cities", "vendors", "products")}
    for name, path in catalogs.items():
        path = ROOT / "catalogs" / f"{name}.txt"
        claim(
            f"catalog size ({name})",
            rf"{anon.entity_count(anon.load_entities(path))} entries",
            "catalogs/README.md",
        )
    # The pair is the way these two lists are meant to be used, so its size is a claim too.
    pair = sum(
        anon.entity_count(anon.load_entities(catalogs[name])) for name in ("vendors", "products")
    )
    claim("catalog pair size", rf"Ticking both lists together adds {pair} entries", "catalogs/README.md")

    # 7. the guard's cap, when its source is reachable (cross-repository)
    guard = next(
        (
            path
            for path in (
                Path.home() / ".pi" / "agent" / "extensions" / "anon-guard.ts",
            )
            if path.is_file()
        ),
        None,
    )
    if guard is None:
        skipped.append("guard cap (12 MB / 20 s): anon-guard.ts not found")
        print("SKIP  guard cap (12 MB / 20 s): anon-guard.ts not found from here", file=sys.stderr)
    else:
        text = guard.read_text(encoding="utf-8")
        size = re.search(r"const MAX_CHECK_BYTES = (\d+) \* 1024 \* 1024", text)
        timeout = re.search(r"const CHECK_TIMEOUT_MS = ([\d_]+)", text)
        if not size or not timeout:
            failures.append(f"guard cap: could not read the constants from {guard}")
        else:
            checked += 1
            expected = f"{size.group(1)} MB / {int(timeout.group(1).replace('_', '')) // 1000} s"
            if expected not in read("docs/DESIGN.md"):
                failures.append(f"guard cap: docs/DESIGN.md does not state {expected!r}")

    if failures:
        for failure in failures:
            print(f"FAIL  {failure}")
        print(
            f"check-doc-numbers: {len(failures)} of {checked} claim(s) drifted from the code"
            + (f" ({len(skipped)} skipped)" if skipped else "")
        )
        return 1
    print(
        f"check-doc-numbers: {checked} claim(s) in the docs match the code"
        + (f" — SKIPPED: {'; '.join(skipped)}" if skipped else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
