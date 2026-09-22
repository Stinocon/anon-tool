#!/usr/bin/env python3
"""convert-fidelity.py — what does `.docx -> Markdown` actually lose? Measured, not guessed.

`docs/DESIGN.md` says office documents are converted to Markdown first because `anon.py` refuses
binary input. If the conversion drops a table, a header or a tracked change, then the redaction
never saw that text either — the loss bounds everything downstream, and it was an open question
("today the answer is unknown", OPEN-ISSUES 25).

Method, and why it is two checks per feature:

  1. the fixture must PROVE it contains the feature (its XML signature in the .docx), otherwise a
     missing marker in the Markdown would say nothing about the converter;
  2. then the marker text is looked for in the converted Markdown.

A feature is only reported as LOST when both hold. Three generators, because no single one writes
everything: `pandoc` (a real writer: styles, tables, links, footnotes, images), `python-docx`
(headers/footers, core properties), and hand-written OOXML for what neither emits (tracked
changes, comments).

    python3 scripts/convert-fidelity.py [--keep]

Local only: no network, no LLM. The fixtures carry no real data and are deleted unless --keep.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONVERT = ROOT / "convert.py"
PANDOC = shutil.which("pandoc")
DOCX_PYTHON = Path.home() / ".pi" / "agent" / "venvs" / "docx-venv" / "bin" / "python"

# feature -> (marker text to look for in the Markdown, XML signature the fixture must contain,
#             part of the .docx the signature lives in, why its absence matters)
#
# The signatures are the ones the writers ACTUALLY emit (inspected: pandoc writes `<w:b />` with a
# space, and expresses a list with `<w:numPr>` rather than a `ListParagraph` style). A guessed
# signature reports "not measurable" for a feature that is measurable, which is the quiet way a
# measurement lies.
FEATURES = {
    "heading 1": ("MarcatoreTitoloUno", b"Heading1", "word/document.xml", "section structure"),
    "bold": ("MarcatoreGrassetto", b"<w:b", "word/document.xml", "emphasis"),
    "italic": ("MarcatoreCorsivo", b"<w:i", "word/document.xml", "emphasis"),
    "bullet list": ("MarcatorePunto", b"<w:numPr>", "word/document.xml", "list structure"),
    "numbered list": ("MarcatoreNumerato", b"<w:numPr>", "word/document.xml", "list structure"),
    "table": ("MarcatoreCella", b"<w:tbl>", "word/document.xml", "tabular data"),
    "hyperlink": ("marcatore-link", b"<w:hyperlink", "word/document.xml", "a link target"),
    "footnote": ("MarcatoreNotaAMargine", b"word/footnotes.xml", "", "small print outside the body"),
    "image": ("MarcatoreImmagine", b"word/media/", "", "a picture (its caption text, not its pixels)"),
    "header": (
        "MarcatoreIntestazione",
        b"word/header1.xml",
        "",
        "a letterhead lives here: it is neither redacted nor regenerated — if this is the finished\n"
        "                      document you deliver, the original .docx still carries it un-redacted",
    ),
    "footer": (
        "MarcatorePiePagina",
        b"word/footer1.xml",
        "",
        "same as the header: outside the pipeline",
    ),
    "tracked insertion": ("MarcatoreInserito", b"<w:ins ", "word/document.xml", "a pending change"),
    "tracked deletion": (
        "MarcatoreCancellato",
        b"<w:del ",
        "word/document.xml",
        "expected: the deleted text is not part of the current document\n"
        "                      (it is also never redacted: it survives in the .docx's revision history)",
    ),
    "comment": ("MarcatoreTestoCommento", b"word/comments.xml", "", "a reviewer's note"),
    "text box": ("MarcatoreCasella", b"txbxContent", "word/document.xml", "a shape's text"),
    "core title": (
        "MarcatoreTitoloDocumento",
        b"<dc:title>",
        "docProps/core.xml",
        "metadata: never redacted, and still in the original .docx and in the PDF you export from it",
    ),
    "core author": (
        "MarcatoreAutoreDocumento",
        b"<dc:creator>",
        "docProps/core.xml",
        "metadata: the author's name is the interesting one",
    ),
}

DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument'
    '.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument'
    '.wordprocessingml.comments+xml"/>'
    "</Types>"
)

DOCX_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    '/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def build_pandoc_fixture(work: Path) -> Path | None:
    """A realistic document: the writer decides how to express each feature."""
    if not PANDOC:
        return None
    image = work / "pixel.png"
    image.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154"
            "789c63000100000500010d0a2db40000000049454e44ae426082"
        )
    )
    source = work / "pandoc.md"
    source.write_text(
        "# MarcatoreTitoloUno\n\n"
        "Testo con **MarcatoreGrassetto** e *MarcatoreCorsivo*.\n\n"
        "- MarcatorePunto uno\n- MarcatorePunto due\n\n"
        "1. MarcatoreNumerato\n\n"
        "| Colonna | Altra |\n|---|---|\n| MarcatoreCella | x |\n\n"
        "[marcatore-link](https://example.com/marcatore-link)\n\n"
        "Nota a piè di pagina.[^1]\n\n[^1]: MarcatoreNotaAMargine\n\n"
        "![MarcatoreImmagine](pixel.png)\n",
        encoding="utf-8",
    )
    target = work / "pandoc.docx"
    done = subprocess.run(
        [PANDOC, str(source), "-o", str(target), f"--resource-path={work}"],
        capture_output=True,
        text=True,
    )
    return target if done.returncode == 0 and target.is_file() else None


def build_docx_fixture(work: Path) -> Path | None:
    """Sections and document properties: python-docx writes them, pandoc does not."""
    if not DOCX_PYTHON.is_file():
        return None
    script = work / "make_docx.py"
    script.write_text(
        """
import sys
from docx import Document

doc = Document()
doc.add_paragraph("Corpo con MarcatoreCorpoDocx.")
doc.sections[0].header.paragraphs[0].text = "MarcatoreIntestazione"
doc.sections[0].footer.paragraphs[0].text = "MarcatorePiePagina"
props = doc.core_properties
props.title = "MarcatoreTitoloDocumento"
props.author = "MarcatoreAutoreDocumento"
doc.save(sys.argv[1])
""",
        encoding="utf-8",
    )
    target = work / "python-docx.docx"
    done = subprocess.run(
        [str(DOCX_PYTHON), str(script), str(target)], capture_output=True, text=True
    )
    return target if done.returncode == 0 and target.is_file() else None


def build_raw_fixture(work: Path) -> Path:
    """Tracked changes, a comment and a text box: neither writer emits these."""
    paragraph = (
        '<w:p><w:r><w:t xml:space="preserve">Prima </w:t></w:r>'
        '<w:ins w:id="1" w:author="revisore" w:date="2026-09-22T10:00:00Z">'
        '<w:r><w:t>MarcatoreInserito </w:t></w:r></w:ins>'
        '<w:del w:id="2" w:author="revisore" w:date="2026-09-22T10:00:00Z">'
        '<w:r><w:delText>MarcatoreCancellato </w:delText></w:r></w:del>'
        '<w:r><w:t>dopo.</w:t></w:r></w:p>'
    )
    commented = (
        '<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>MarcatoreCommento</w:t></w:r>'
        '<w:commentRangeEnd w:id="0"/>'
        '<w:r><w:commentReference w:id="0"/></w:r></w:p>'
    )
    textbox = (
        '<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml" style="width:100pt;'
        'height:50pt"><v:textbox><w:txbxContent><w:p><w:r><w:t>MarcatoreCasella</w:t></w:r>'
        "</w:p></w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:v="urn:schemas-microsoft-com:vml"><w:body>'
        f"{paragraph}{commented}{textbox}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
        "</w:body></w:document>"
    )
    comments = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:comment w:id="0" w:author="revisore"><w:p><w:r><w:t>MarcatoreTestoCommento</w:t></w:r></w:p>'
        "</w:comment></w:comments>"
    )
    target = work / "raw.docx"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", DOCX_CONTENT_TYPES)
        archive.writestr("_rels/.rels", DOCX_RELS)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/comments.xml", comments)
    return target


def in_docx(path: Path, signature: bytes, part: str) -> bool:
    """Does the .docx really carry the feature? Without this, a missing marker proves nothing."""
    try:
        with zipfile.ZipFile(path) as archive:
            if part == "":  # the signature is a part NAME
                return any(signature.decode() in name for name in archive.namelist())
            if part not in archive.namelist():
                return False
            return signature in archive.read(part)
    except (OSError, zipfile.BadZipFile):
        return False


def convert(path: Path) -> tuple[bool, str]:
    done = subprocess.run([sys.executable, str(CONVERT), str(path)], capture_output=True, text=True)
    return done.returncode == 0 and bool(done.stdout.strip()), done.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the fixture .docx files")
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="convert-fidelity-"))
    print(f"fixtures in {work}\n")
    if not PANDOC:
        print("note: pandoc not found — the realistic fixture is skipped, visibly", file=sys.stderr)
    if not DOCX_PYTHON.is_file():
        print(f"note: {DOCX_PYTHON} not found — the sections/properties fixture is skipped", file=sys.stderr)

    fixtures = [
        ("pandoc", build_pandoc_fixture(work)),
        ("python-docx", build_docx_fixture(work)),
        ("hand-written OOXML", build_raw_fixture(work)),
    ]
    markdown: dict[str, str] = {}
    for name, path in fixtures:
        if path is None:
            continue
        ok, text = convert(path)
        markdown[name] = text if ok else ""
        print(f"{name}: {path.name} -> {'converted' if ok else 'CONVERSION FAILED'}")

    print(f"\n{'feature':<20} {'in the .docx':<13} {'in the Markdown':<16} verdict")
    print("-" * 72)
    lost: list[str] = []
    for feature, (marker, signature, part, note) in FEATURES.items():
        presence = {name: path for name, path in fixtures if path is not None and in_docx(path, signature, part)}
        if not presence:
            print(f"{feature:<20} {'no':<13} {'-':<16} not measurable (no fixture emits it)")
            continue
        found = [name for name, path in presence.items() if marker in markdown.get(name, "")]
        if found:
            print(f"{feature:<20} {'yes':<13} {'yes':<16} preserved ({', '.join(found)}, {note})")
        else:
            lost.append(feature)
            print(f"{feature:<20} {'yes':<13} {'NO':<16} NOT CARRIED OVER: {note}")
    print(
        f"\n{len(FEATURES) - len(lost)} of {len(FEATURES)} features measured as preserved"
        + (f"; not carried over: {', '.join(lost)}" if lost else "")
    )
    if lost:
        # The measurement is what motivated the in-place path, so it must point at the answer:
        # those features are not lost any more, they are rewritten where they live.
        print(
            "\nThose features are not redacted by converting — which is the point: the container\n"
            "pass rewrites them inside the file (`anon.py verbale.docx` -> `verbale.redacted.docx`,\n"
            "verified on the output), and the guard keeps using the conversion because its path is\n"
            "a `read`, not a delivery."
        )
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"kept: {work}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
