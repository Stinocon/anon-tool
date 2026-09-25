#!/usr/bin/env python3
"""pdfout.py — text into a plain PDF, standard library only, on this machine.

A redacted document is often DELIVERED as a PDF, and `anon.py` refuses a PDF for in-place
redaction (there is no PDF container path: rewriting a content stream is where a redaction bug
becomes a document that *looks* redacted). The honest alternative is a NEW PDF built from the
already-redacted text: the LAYOUT is not preserved, the substance is, and nothing is claimed that
the tool cannot verify.

    pdfout.py redacted.md > redacted.pdf
    pdfout.py redacted.md -o redacted.pdf

Text only: base-14 Helvetica, WinAnsi (so the Italian accented letters survive), simple word wrap,
one text object per page. No images, no links, no metadata beyond the trailer. It is deliberately
not a converter: `convert.py` (anydoc) reads documents; this writes one.

Stdlib only, no network. It writes what it is given; it does not redact. The caller redacts.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PAGE_WIDTH = 595  # A4 at 72 dpi
PAGE_HEIGHT = 842
MARGIN = 48
LEADING = 14.0
FONT_SIZE = 10
MAX_CHARS = 92  # ~ Helvetica 10pt across the text width; a wrap, not a layout engine

_ESCAPE = {0x5C: b"\\\\", 0x28: b"\\(", 0x29: b"\\)"}


def _pdf_bytes(line: str) -> bytes:
    """The bytes of one text line inside a PDF string literal, escaped.

    WinAnsi (cp1252) covers the accented letters an Italian document actually uses; anything
    outside it becomes `?` rather than being dropped, so a missing glyph is visible in the output.
    """
    out = bytearray()
    for byte in line.encode("cp1252", "replace"):
        out += _ESCAPE.get(byte, bytes((byte,)))
    return bytes(out)


def _wrap(text: str) -> list[str]:
    """Word wrap, then a hard cut for a word longer than the line (a URL, a hash)."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw:
            lines.append("")
            continue
        current = ""
        for word in raw.split(" "):
            candidate = f"{current} {word}" if current else word
            if len(candidate) <= MAX_CHARS:
                current = candidate
                continue
            if current:
                lines.append(current)
            while len(word) > MAX_CHARS:
                lines.append(word[:MAX_CHARS])
                word = word[MAX_CHARS:]
            current = word
        lines.append(current)
    return lines


def _content(lines: list[str]) -> bytes:
    """One page's content stream: Helvetica, leading TL, `T*` between lines."""
    parts = [
        b"BT",
        f"/F1 {FONT_SIZE} Tf".encode(),
        f"{LEADING:g} TL".encode(),
        f"{MARGIN} {int(PAGE_HEIGHT - MARGIN - FONT_SIZE)} Td".encode(),
    ]
    for index, line in enumerate(lines):
        if index:
            parts.append(b"T*")
        parts.append(b"(" + _pdf_bytes(line) + b") Tj")
    parts.append(b"ET")
    return b"\n".join(parts)


def build_pdf(text: str) -> bytes:
    """A complete PDF 1.4 document that shows `text`. Deterministic: same text, same bytes."""
    wrapped = _wrap(text)
    per_page = max(1, int((PAGE_HEIGHT - 2 * MARGIN) // LEADING))
    pages = [wrapped[i:i + per_page] for i in range(0, len(wrapped), per_page)] or [[]]

    # 1 = catalog, 2 = pages, 3 = font, then one Page + one Contents per page.
    objects: list[bytes] = [b""] * (3 + 2 * len(pages) + 1)
    page_ids = [4 + 2 * index for index in range(len(pages))]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    for index, lines in enumerate(pages):
        page_id = 4 + 2 * index
        content_id = page_id + 1
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        stream = _content(lines)
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")  # the binary comment marks it as binary
    offsets = [0] * len(objects)
    for number in range(1, len(objects)):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects)}\n".encode()
    out += b"0000000000 65535 f \n"
    for number in range(1, len(objects)):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects)} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pdfout.py", description="text -> a plain, text-only PDF")
    parser.add_argument("file", nargs="?", help="text/Markdown file (default: stdin)")
    parser.add_argument("-o", "--output", help="output .pdf (default: stdout)")
    args = parser.parse_args(argv)
    try:
        text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    except (OSError, UnicodeDecodeError) as error:
        print(f"pdfout: {error}", file=sys.stderr)
        return 2
    data = build_pdf(text)
    if args.output:
        Path(args.output).write_bytes(data)
    else:
        sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
