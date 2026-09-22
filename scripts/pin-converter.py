#!/usr/bin/env python3
"""pin-converter.py — emit the hashed requirements file for the document converter.

`anon-tool` installs the converter (`firecrawl-anydoc`) in two places: inside the container and,
for a native install, in a pinned venv created by `convert.py --install`. A version pin alone
still trusts the index to hand back the same bytes; `pip install --require-hashes` does not.

The digests are taken from the package index's published metadata, so the file is reproducible:

    python3 scripts/pin-converter.py > requirements-anydoc.txt

Every published artifact of the version is listed — the wheels for each platform plus the sdist —
because the installer picks the one that fits its OS/arch and verifies THAT one's digest. One file
therefore serves both the Debian container and a macOS venv.

It also checks the dependency CLOSURE: `--require-hashes` demands a digest for every package pip
installs, so a converter version that grows a dependency would break both the image build and
`convert.py --install`. The check is a dry-run resolve through pip, and it refuses to emit a file
that would fail that way.

Network: this script contacts the index (`--index`, default PyPI) and pip. It is a development
tool; the tool itself never needs the network.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

DEFAULT_PACKAGE = "firecrawl-anydoc"
DEFAULT_VERSION = "0.2.3"
DEFAULT_INDEX = "https://pypi.org/pypi"


def fetch(package: str, version: str, index: str) -> dict:
    url = f"{index.rstrip('/')}/{package}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - fixed https scheme
        return json.load(response)


def resolve_closure(requirement: str) -> set[str] | None:
    """The package names pip would install for `requirement`, or None when that cannot be checked."""
    with tempfile.TemporaryDirectory(prefix="pin-converter-") as work:
        report = Path(work) / "report.json"
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", "--ignore-installed",
             "--report", str(report), requirement],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not report.is_file():
            return None
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return {
            str(item.get("metadata", {}).get("name", "")).lower().replace("_", "-")
            for item in data.get("install", [])
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pin-converter.py", description="emit hashed requirements")
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    args = parser.parse_args(argv)

    metadata = fetch(args.package, args.version, args.index)
    artifacts = sorted(
        (item for item in metadata.get("urls") or [] if item.get("packagetype") in ("bdist_wheel", "sdist")),
        key=lambda item: item["filename"],
    )
    if not artifacts:
        print(f"pin-converter: no installable artifact for {args.package} {args.version}", file=sys.stderr)
        return 2

    # Checked BEFORE anything is printed, so a refusal never leaves a half-written file.
    package = args.package.lower().replace("_", "-")
    closure = resolve_closure(f"{args.package}=={args.version}")
    if closure is None:
        print(
            "pin-converter: pip could not resolve the dependency closure — NOT verified "
            "(a new transitive dependency would break --require-hashes at build time)",
            file=sys.stderr,
        )
    else:
        extra = sorted(closure - {package})
        if extra:
            print(
                f"pin-converter: {args.package} {args.version} also installs {', '.join(extra)}.\n"
                "  --require-hashes needs a digest for EVERY package, so they must be pinned here\n"
                "  as well: add them to this script and regenerate the file.",
                file=sys.stderr,
            )
            return 2

    print("# Pinned converter dependency. Install with:")
    print("#   pip install --require-hashes -r requirements-anydoc.txt")
    print("#")
    print(f"# Regenerate with: python3 scripts/pin-converter.py --version {args.version}")
    print(f"# Source: {args.index.rstrip('/')}/{args.package}/{args.version}/json (sha256 as published)")
    print("# Every published artifact is listed: pip picks the one for this platform and checks it.")
    # One logical line: a `#` comment inside a backslash continuation ENDS the requirement for
    # pip (comments are stripped before continuations are joined), which silently detached every
    # hash but the first. The artifacts are therefore listed after the requirement, in order.
    print(f"{args.package}=={args.version} \\")
    for index, item in enumerate(artifacts):
        continuation = "" if index == len(artifacts) - 1 else " \\"
        print(f"    --hash=sha256:{item['digests']['sha256']}{continuation}")
    print("#")
    print("# The digests above, in the same order:")
    for item in artifacts:
        print(f"#   {item['filename']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
