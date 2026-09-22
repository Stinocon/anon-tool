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
     never silent: a document that still contains `[EMAIL-3]` is not deliverable. A placeholder
     that Word split across two runs (`[EMAIL-` + `3]` in two text nodes) is REPAIRED
     automatically; what is left is a fragment split across two PARTS, or a hand-edited token.

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

# One source of truth for the placeholder syntax and the container sniffing: the engine itself.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import anon  # noqa: E402  (path set above on purpose)

VERSION = anon.VERSION  # single source of truth: the product version lives in anon.py

# The container primitives live in the ENGINE (`anon.py`): one implementation for both directions
# — writing a placeholder in, restoring a value out — because two copies of "what a text part is"
# would drift silently. They are re-bound here so this module reads exactly as before.
MARKUP_RE = anon.MARKUP_RE
XML_SUFFIXES = anon.XML_SUFFIXES
visible_text = anon.visible_text
decode_part = anon.decode_part
is_xml_part = anon.is_xml_part
is_text_part = anon.is_text_part
xml_protect = anon.xml_protect
visible_index = anon.visible_index
write_container_atomic = anon.write_container_atomic
# The same boundary rule the redaction uses, from the same source: what may be joined is a property
# of the document, not of the direction it is being read in.
masked_index = anon.masked_index
path_at_source = anon.path_at_source
boundary_offenders = anon.boundary_offenders

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


def repair_split_placeholders(
    text: str,
    entries: dict[str, dict[str, str]],
    protect: "callable | None" = None,
) -> tuple[str, int]:
    """Restore placeholders that a word processor split across two runs.

    The placeholder is written in fragments in separate text nodes, so `deanonize` found nothing
    contiguous to replace. Merging those nodes into one would move the formatting of whatever
    they carry, so instead the real value goes in the FIRST fragment and the remaining fragments
    are emptied: no markup is added or removed, and the part stays well-formed. Only characters of
    the placeholder itself are ever deleted.

    A placeholder whose fragments live in DIFFERENT containers is left alone. Emptying the others
    there would move the value into the first one and delete the second paragraph's (or table cell's)
    text: the value would come back in the wrong place. Leaving it is not silent — the caller's
    verdict is computed from the OUTPUT, so the placeholder is reported as unrestorable.

    Returns (repaired text, number of placeholders repaired).
    """
    if not entries:
        return text, 0
    visible, offsets = visible_index(text)
    known = [placeholder for placeholder in entries if placeholder in visible]
    if not known:
        return text, 0
    # Longest placeholder first (as in `deanonize`), so `[EMAIL-10]` is never read as `[EMAIL-1`.
    known.sort(key=len, reverse=True)

    edits: list[tuple[int, int, str]] = []
    repaired = 0
    segments: list[tuple[int, int, tuple]] | None = None
    cursor = 0
    while cursor < len(visible):
        placeholder = next((item for item in known if visible.startswith(item, cursor)), None)
        if placeholder is None:
            cursor += 1
            continue
        raw = offsets[cursor:cursor + len(placeholder)]
        cursor += len(placeholder)
        runs: list[list[int]] = []
        for position in raw:
            if runs and position == runs[-1][-1] + 1:
                runs[-1].append(position)
            else:
                runs.append([position])
        if len(runs) == 1:
            continue  # contiguous: the normal pass already replaced it
        if segments is None:
            # Computed once, and only when a split actually has to be judged.
            _visible, _offsets, segments = masked_index(text)
        offenders = []
        for other in runs[1:]:
            offenders += boundary_offenders(path_at_source(segments, runs[0][0]),
                                            path_at_source(segments, other[0]))
        if offenders:
            continue  # two containers: leaving it is reported, not hidden — see the docstring
        value = entries[placeholder].get("original")
        if value is None:
            continue
        # The FIRST fragment is replaced by the value, the other fragments are emptied: only
        # characters of the placeholder itself are ever touched.
        edits.append((runs[0][0], runs[0][-1] + 1, protect(value) if protect else value))
        repaired += 1
        for run in runs[1:]:
            edits.append((run[0], run[-1] + 1, ""))

    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, repaired


def count_placeholders(text: str, entries: dict[str, dict[str, str]]) -> tuple[int, int]:
    """(still-restorable placeholders, placeholder-shaped tokens not in the map)."""
    known = sum(text.count(placeholder) for placeholder in entries)
    unknown = sum(1 for found in anon.PLACEHOLDER_RE.finditer(text) if found.group(0) not in entries)
    return known, unknown


def _read_map_object(path: Path) -> dict:
    """The map file parsed and required to be a JSON object (the schema's outer level).

    Through `anon.read_json_object`, because a deeply nested map file used to raise
    `RecursionError` past the CLI's `except ValueError` (traceback, exit 1) and past the API's
    400 path (500). One parser, one failure type.
    """
    return anon.read_json_object(path.read_text(encoding="utf-8"), str(path))


def _validate_entries(path: Path, entries: object) -> dict[str, dict[str, str]]:
    """The `entries` object, validated ONE level deeper than a shape check.

    A map is an operator-editable file, and every consumer (CLI restore, `/api/deanonymize`,
    `/api/maps/reveal`, the UI listing) must see the same guarantees. Checking `isinstance(…, dict)`
    here and then calling `.get()` on the VALUES is how the same defect came back three times, so
    the validation is done once, at this boundary, for every entry:

        {"[EMAIL-1-a3f9]": {"type": "EMAIL", "original": "someone@example.com"}}

    A null/string/list value, or an `original` that is not a string or null, is a `ValueError`
    naming the offending key — never an `AttributeError` in the middle of a restore.
    """
    if not isinstance(entries, dict):
        raise ValueError(f"{path}: not an anon map (missing 'entries')")
    validated: dict[str, dict[str, str]] = {}
    for key, value in entries.items():
        _check_entry(path, key, value)
        kind = value.get("type")
        # `type` is informational (the UI shows it); a missing or odd value is normalized rather
        # than refused, because refusing a map that restores correctly would be the wrong trade.
        validated[key] = {"type": kind if isinstance(kind, str) else "ALTRO", "original": value.get("original")}
    return validated


def _check_entry(path: Path, key: object, value: object) -> None:
    """The per-entry rule, in ONE place: the restore path and the listing both apply it."""
    if not isinstance(key, str) or not isinstance(value, dict):
        raise ValueError(f"{path}: malformed map entry {key!r} (expected an object)")
    original = value.get("original")
    if original is not None and not isinstance(original, str):
        raise ValueError(f"{path}: malformed 'original' for {key!r} (expected a string or null)")


def _count_entries(path: Path, entries: object) -> int:
    """Validate every entry and return how many there are, WITHOUT copying them.

    The listing only needs the COUNT: building a validated copy of a large map just to call
    `len()` was O(N) allocations per map, up to 100 maps per request.
    """
    if not isinstance(entries, dict):
        raise ValueError(f"{path}: not an anon map (missing 'entries')")
    for key, value in entries.items():
        _check_entry(path, key, value)
    return len(entries)


def load_map(path: Path) -> dict[str, dict[str, str]]:
    """The placeholder → real-value mapping, validated (see `_validate_entries`)."""
    return _validate_entries(path, _read_map_object(path).get("entries"))


def load_map_metadata(path: Path) -> dict[str, object]:
    """The listing fields of a map, validated — the ONE reader the web UI uses as well.

    Returns `{id, created, source, counts, entries}` with types a caller can rely on. `source` is
    reduced to a basename WITHOUT `pathlib`: a hand-edited string containing an embedded NUL made
    `Path()` raise, which turned a read-only listing into a 500.
    """
    data = _read_map_object(path)
    source = data.get("source")
    basename = ""
    if isinstance(source, str):
        basename = source.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    created = data.get("created")
    counts = data.get("counts")
    return {
        "id": str(data.get("id") or path.stem.replace(".map", "")),
        "created": created if isinstance(created, str) else None,
        "source": basename,
        "counts": counts if isinstance(counts, dict) else None,
        # Validated with the same RULE the restore path uses, without building a copy of it.
        "entries": _count_entries(path, data.get("entries")),
    }




def deanon_container(source: Path, output: Path, entries: dict[str, dict[str, str]]) -> dict:
    """Rewrite every text part of an office container, then verify what was actually written."""
    entries = restorable(entries)
    chunks: list[tuple[zipfile.ZipInfo, bytes]] = []
    parts: dict[str, int] = {}
    repairs = 0
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            blob = archive.read(info)
            text, codec = decode_part(blob)
            if text is not None:
                protect = xml_protect if is_xml_part(info.filename) else None
                text, replaced = deanonize(text, entries, protect)
                text, repaired = repair_split_placeholders(text, entries, protect)
                if replaced or repaired:
                    parts[info.filename] = parts.get(info.filename, 0) + replaced + repaired
                    repairs += repaired
                blob = text.encode(codec or "utf-8", "surrogateescape")
            chunks.append((info, blob))
    write_container_atomic(output, chunks)

    # The verdict must come from the OUTPUT, not from our intent, and it must be computed on the
    # VISIBLE text of every text part (a split placeholder is invisible in the raw XML; an
    # embedded .txt is not an .xml part at all).
    remaining = 0
    unknown = 0
    remaining_parts: list[str] = []
    unreadable_parts: list[str] = []
    with zipfile.ZipFile(output) as written:
        for name in written.namelist():
            text, _codec = decode_part(written.read(name))
            if text is None:
                if is_text_part(name):
                    # A part named as text that cannot be read is not "clean": the verdict would be
                    # computed on a part nobody looked at. Fail closed, exactly like the redaction.
                    unreadable_parts.append(name)
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
        "repaired": repairs,
        "remaining": remaining,
        "remaining_parts": remaining_parts,
        "unreadable_parts": unreadable_parts,
        "unknown_placeholders": unknown,
        "map_entries": len(entries),
        # Fail CLOSED: an unresolved `[EMAIL-7]` is not deliverable even though the map has no
        # such key — it means the document and the map do not belong together.
        "complete": remaining == 0 and unknown == 0 and not unreadable_parts and bool(parts),
    }


def deanon_text(source: Path, output: Path, entries: dict[str, dict[str, str]]) -> dict:
    entries = restorable(entries)
    text = source.read_text(encoding="utf-8", errors="replace")
    restored, replaced = deanonize(text, entries)
    restored, repaired = repair_split_placeholders(restored, entries)
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
        "repaired": repaired,
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
        # Through the same helper, not a bare `json.loads`: this read only serves provenance, and
        # relying on "the parse above already validated the bytes" makes correctness depend on the
        # order of two statements.
        raw_map = anon.read_json_object(map_path.read_text(encoding="utf-8"), str(map_path))
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
        # An unreadable part is the reason `complete` can be false with nothing else to show: without
        # this line the operator sees "NOTHING RESTORED" and looks at the wrong thing.
        if report.get("unreadable_parts"):
            print(
                "deanon: a part named as text could not be read, so the result cannot be called "
                "complete:"
            )
            for name in report["unreadable_parts"]:
                print(f"        {name}")
            print("        A document nobody could fully read is not a document confirmed restored.")
        print(f"deanon: -> {out}")

    # Never silenced — not by --quiet, not by --json: an undeliverable document is an error
    # condition, and stderr is the right channel for it (stdout stays machine-readable).
    if remaining:
        where = ", ".join(report.get("remaining_parts") or []) or "-"
        print(
            f"deanon: INCOMPLETE — {remaining} placeholder(s) this map produced are still visible\n"
            f"        in the output ({where}). A document still containing them is NOT\n"
            "        deliverable. A placeholder split inside ONE text part is repaired\n"
            "        automatically, so what is left is either a fragment split across two\n"
            "        parts (document + header), a placeholder edited by hand, or a document\n"
            "        that does not belong to this map. Fix: regenerate it from the Markdown\n"
            "        (`pandoc final.md -o out.docx`) instead of editing the .docx by hand,\n"
            "        then run deanon again.",
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
