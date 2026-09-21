#!/usr/bin/env python3
"""deanon.py — restore the real values into a document produced from a redacted source.

`anon.py` writes a redacted copy plus a reversible map under `~/.anon/maps/`. This script is the
inverse: it takes the FINISHED document — the report written from the redacted text — and puts
the original values back, so the client receives a document that still carries the real names,
emails, IPs and so on.

Two input shapes, one command:

  * **Plain text** (``.md .txt .csv .json .yaml ...``) — substituted directly.
  * **Office container** (``.docx .xlsx .pptx .odt``) — a ZIP of XML parts; every text part is
    rewritten in place (document, headers, footers, footnotes, comments, document properties,
    and embedded text parts), keeping the archive's entry order, count and compression so the
    file stays valid and openable.

    deanon.py REPORT.docx ~/.anon/maps/<id>.map.json
    deanon.py REPORT.docx <map> --out REPORT-final.docx

Exit codes
  0  every placeholder was replaced
  2  error (unreadable/empty document, unparsable map, unusable arguments)
  3  PARTIAL — some placeholders this map produced are still visible in the output. Reported,
     never silent: a document that still contains `[EMAIL-3]` is not deliverable. The usual cause
     is Word splitting a run (`[EMAIL-` + `3]` in two text nodes), so no contiguous string was
     there to replace.

Design notes
  * Placeholders are replaced longest-first, so `[EMAIL-10]` can never be partially rewritten by
    `[EMAIL-1]`.
  * Only placeholders present in the map are touched: an unknown `[EMAIL-9]` written by the model
    is left alone (it is a hallucinated reference, not data to restore) and reported as `unknown`.
  * **A value written into an XML part is XML-escaped.** `Acme & Söhne GmbH` must become
    `Acme &amp; Söhne GmbH` or the part stops being well-formed XML and Word refuses to open the
    document — while a naive `str.replace` would report success.
  * **The verdict is computed on the OUTPUT**, over the tag-stripped visible text of *every* text
    part, not over the raw XML: a placeholder split across two runs, or sitting in an embedded
    `.txt`, is invisible to a naive check yet plainly visible to a reader.
  * The output is written to a temporary file and moved into place atomically, so an interrupted
    run can never leave a truncated document (and `--out <same file>` is safe).
  * Local and deterministic: stdlib only, no network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as _sax_escape

# One source of truth for the placeholder syntax and the container sniffing: the engine itself.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import anon  # noqa: E402  (path set above on purpose)

VERSION = "1.2.0"

# Markup stripper used by the VERIFICATION step. Verifying on the raw XML is wrong: Word splits a
# placeholder across runs (`<w:t>[EMAIL-</w:t></w:r><w:r><w:t>1]</w:t>`), so the contiguous string
# is absent from the raw bytes even though the document visibly still contains `[EMAIL-1]`.
MARKUP_RE = re.compile(r"<[^>]*>")
XML_SUFFIXES = (".xml", ".rels")

# A ZIP part can legally be UTF-16/UTF-32 encoded (OOXML allows it); decoded as UTF-8 its
# placeholders are NUL-interleaved and would be neither replaced nor detected.
_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def visible_text(xml: str) -> str:
    """The text a reader of the document would see, approximated by dropping every tag."""
    return MARKUP_RE.sub("", xml)


def decode_part(blob: bytes) -> tuple[str | None, str | None]:
    """(text, codec) for a text part, or (None, None) when the part is binary.

    Binary is decided by content, not by filename: `word/embeddings/note.txt` holds text that
    must be rewritten, and an image is binary whatever it is called.
    """
    for bom, codec in _BOMS:
        if blob.startswith(bom):
            return blob.decode(codec, "surrogateescape"), codec
    if b"\x00" in blob[:8192]:
        return None, None
    return blob.decode("utf-8", "surrogateescape"), "utf-8"


def is_xml_part(name: str) -> bool:
    return name == "[Content_Types].xml" or name.lower().endswith(XML_SUFFIXES)


def xml_protect(value: str) -> str:
    """Escape a real value before writing it into an XML part.

    `&`, `<`, `>` are mandatory in element text; `"` and `'` matter when the placeholder sits in
    an attribute value (`w:tooltip="..."`, a `.rels` Target). Escaping quotes in element text is
    harmless — they decode back to themselves — so this is safe in both contexts.
    """
    return _sax_escape(value, {'"': "&quot;", "'": "&apos;"})


def restorable(entries: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Only entries that actually carry a value: a key with `original: null` cannot be restored."""
    return {key: value for key, value in entries.items() if value.get("original") is not None}


def deanonize(
    text: str,
    entries: dict[str, dict[str, str]],
    protect: "callable | None" = None,
) -> tuple[str, int]:
    """Replace placeholders with their originals, longest placeholder first.

    `protect` transforms the replacement value (XML escaping when writing into an XML part).
    """
    restored = 0
    for placeholder in sorted(entries, key=len, reverse=True):
        value = entries[placeholder].get("original")
        if value is None or placeholder not in text:
            continue
        restored += text.count(placeholder)
        text = text.replace(placeholder, protect(value) if protect else value)
    return text, restored


def count_placeholders(text: str, entries: dict[str, dict[str, str]]) -> tuple[int, int]:
    """(still-restorable placeholders, placeholder-shaped tokens not in the map)."""
    known = sum(text.count(placeholder) for placeholder in entries)
    unknown = sum(1 for found in anon.PLACEHOLDER_RE.finditer(text) if found.group(0) not in entries)
    return known, unknown


def load_map(path: Path) -> dict[str, dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise ValueError(f"{path}: not an anon map (missing 'entries')")
    return entries


def _write_atomic(output: Path, chunks: list[tuple[zipfile.ZipInfo, bytes]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{os.urandom(4).hex()}")
    try:
        with zipfile.ZipFile(temporary, "w") as target:
            for info, blob in chunks:
                # A fresh ZipInfo: reusing the original object can carry a data-descriptor flag
                # bit that zipfile then rewrites inconsistently.
                clean = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                clean.compress_type = info.compress_type
                clean.external_attr = info.external_attr
                clean.internal_attr = info.internal_attr
                clean.create_system = info.create_system
                target.writestr(clean, blob)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def deanon_container(source: Path, output: Path, entries: dict[str, dict[str, str]]) -> dict:
    """Rewrite every text part of an office container, then verify what was actually written."""
    entries = restorable(entries)
    chunks: list[tuple[zipfile.ZipInfo, bytes]] = []
    parts: dict[str, int] = {}
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            blob = archive.read(info)
            text, codec = decode_part(blob)
            if text is not None:
                protect = xml_protect if is_xml_part(info.filename) else None
                text, replaced = deanonize(text, entries, protect)
                if replaced:
                    parts[info.filename] = parts.get(info.filename, 0) + replaced
                blob = text.encode(codec or "utf-8", "surrogateescape")
            chunks.append((info, blob))
    _write_atomic(output, chunks)

    # The verdict must come from the OUTPUT, not from our intent, and it must be computed on the
    # VISIBLE text of every text part (a split placeholder is invisible in the raw XML; an
    # embedded .txt is not an .xml part at all).
    remaining = 0
    unknown = 0
    remaining_parts: list[str] = []
    with zipfile.ZipFile(output) as written:
        for name in written.namelist():
            text, _codec = decode_part(written.read(name))
            if text is None:
                continue
            visible = visible_text(text)
            known_count, unknown_count = count_placeholders(visible, entries)
            if known_count:
                remaining += known_count
                remaining_parts.append(name)
            unknown += unknown_count

    return {
        "schema": anon.SCHEMA,
        "format": "container",
        "parts": parts,
        "replaced": sum(parts.values()),
        "remaining": remaining,
        "remaining_parts": remaining_parts,
        "unknown_placeholders": unknown,
        "map_entries": len(entries),
        # Fail CLOSED: an unresolved `[EMAIL-7]` is not deliverable even though the map has no
        # such key — it means the document and the map do not belong together.
        "complete": remaining == 0 and unknown == 0 and bool(parts),
    }


def deanon_text(source: Path, output: Path, entries: dict[str, dict[str, str]]) -> dict:
    entries = restorable(entries)
    text = source.read_text(encoding="utf-8", errors="replace")
    restored, replaced = deanonize(text, entries)
    remaining, unknown = count_placeholders(restored, entries)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{os.urandom(4).hex()}")
    try:
        temporary.write_text(restored, encoding="utf-8")
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "schema": anon.SCHEMA,
        "format": "text",
        "parts": {source.name: replaced},
        "replaced": replaced,
        "remaining": remaining,
        "remaining_parts": [source.name] if remaining else [],
        "unknown_placeholders": unknown,
        "map_entries": len(entries),
        "complete": remaining == 0 and unknown == 0 and replaced > 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deanon.py",
        description="Restore the real values in a document produced from an anonymized source.",
    )
    parser.add_argument("document", help="document to de-anonymize (text or .docx/.xlsx/.pptx/.odt)")
    parser.add_argument("map", help="map produced by anon.py (~/.anon/maps/<id>.map.json)")
    parser.add_argument("--out", help="write here (default: <name>.deanon.<ext>)")
    parser.add_argument("--stdout", action="store_true", help="write to stdout (text documents only)")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--version", action="version", version=f"deanon.py {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    doc = Path(args.document).expanduser()
    map_path = Path(args.map).expanduser()
    if not doc.is_file():
        print(f"deanon: not a file: {doc}", file=sys.stderr)
        return 2
    if doc.stat().st_size == 0:
        print(f"deanon: {doc} is empty — nothing to restore", file=sys.stderr)
        return 2
    if not map_path.is_file():
        print(f"deanon: map not found: {map_path}", file=sys.stderr)
        return 2
    try:
        entries = load_map(map_path)
        raw_map = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"deanon: {exc}", file=sys.stderr)
        return 2

    container = anon.sniff(doc) == "container"
    if args.stdout and container:
        print(f"deanon: --stdout is for text documents; {doc.name} is a container", file=sys.stderr)
        return 2
    if args.stdout:
        original = doc.read_text(encoding="utf-8", errors="replace")
        text, replaced = deanonize(original, restorable(entries))
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        remaining, unknown = count_placeholders(text, restorable(entries))
        if remaining or unknown or not replaced:
            print(
                "deanon: INCOMPLETE — "
                f"{remaining} unresolved, {unknown} unknown placeholder(s), {replaced} restored.",
                file=sys.stderr,
            )
            return 3
        return 0

    out = Path(args.out).expanduser() if args.out else doc.with_name(f"{doc.stem}.deanon{doc.suffix}")
    try:
        report = deanon_container(doc, out, entries) if container else deanon_text(doc, out, entries)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        print(f"deanon: failed: {exc}", file=sys.stderr)
        return 2

    report["document"] = str(doc)
    report["output"] = str(out)
    report["map"] = str(map_path)
    # Provenance: five minutes later, `map_source` is the only thing that tells an operator which
    # run produced this map. Placeholder numbering is per-document, so a wrong map cannot be
    # detected from the content alone — it has to be visible.
    report["map_source"] = raw_map.get("source")
    report["map_created"] = raw_map.get("created")
    report["map_counts"] = raw_map.get("counts")
    remaining = int(report["remaining"])
    unknown = int(report["unknown_placeholders"])

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    elif not args.quiet:
        print(f"deanon: {report['replaced']} placeholder occurrence(s) restored ({report['format']})")
        for name, count in sorted(dict(report.get("parts") or {}).items()):
            print(f"        {name}: {count}")
        if report["map_source"]:
            print(f"deanon: mappa del {report['map_created'] or '?'} da {report['map_source']}")
        if report["unknown_placeholders"]:
            print(f"deanon: {report['unknown_placeholders']} placeholder-shaped token(s) not in the map — left as-is")
        print(f"deanon: -> {out}")

    # Never silenced — not by --quiet, not by --json: an undeliverable document is an error
    # condition, and stderr is the right channel for it (stdout stays machine-readable).
    if remaining:
        where = ", ".join(report.get("remaining_parts") or []) or "-"
        print(
            f"deanon: INCOMPLETE — {remaining} placeholder(s) this map produced are still visible\n"
            f"        in the output ({where}). A document still containing them is NOT\n"
            "        deliverable. Usual cause: Word split a placeholder across two text runs\n"
            "        (`[EMAIL-` + `3]`), so no contiguous string was there to replace.\n"
            "        Fix: regenerate the document from the Markdown (`pandoc final.md -o out.docx`)\n"
            "        instead of editing the .docx by hand, then run deanon again.",
            file=sys.stderr,
        )
    if not remaining and unknown:
        print(
            f"deanon: INCOMPLETE — {unknown} placeholder-shaped token(s) are not in this map.\n"
            "        Either the document comes from a different run (wrong map), or it contains\n"
            "        tokens that were never produced here. Do NOT deliver it: those values are\n"
            "        still placeholders.",
            file=sys.stderr,
        )
    if report["replaced"] == 0 and not remaining and not unknown:
        print(
            "deanon: NOTHING RESTORED — no placeholder from this map was found in the document.\n"
            "        Is this the document produced from that redacted source, and is this the\n"
            "        right map? Delivering it as-is would ship placeholders or drop the real\n"
            "        values silently.",
            file=sys.stderr,
        )

    return 0 if report["complete"] else 3


if __name__ == "__main__":
    sys.exit(main())
