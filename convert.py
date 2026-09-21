#!/usr/bin/env python3
"""convert.py — document → Markdown, locally, for the anonymizer.

`anon.py` refuses binary documents on purpose (reading a .docx as text would redact almost
nothing and leave a corrupted copy named "redacted"). Converting to Markdown first is therefore
part of the pipeline, and this is the tool's own converter: it does not depend on a Pi
installation.

    convert.py FILE            # Markdown on stdout
    convert.py --doctor        # is a converter available?

Resolution order for the `anydoc` engine:
  1. importable in the running interpreter (the container installs it, `pip install
     firecrawl-anydoc==0.2.3`);
  2. otherwise a pinned venv — `$ANON_CONVERTER_PYTHON`, else `~/.pi/agent/venvs/anydoc-venv`
     (the venv the `docs` skill provisions), created on demand with `--install`.

Local only: no network at conversion time (only `--install` uses pip).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PIN = "firecrawl-anydoc==0.2.3"
DEFAULT_VENV = Path.home() / ".pi" / "agent" / "venvs" / "anydoc-venv"

_CONVERT_CODE = r"""
import sys
import anydoc
try:
    sys.stdout.write(anydoc.to_markdown(sys.argv[1]))
except anydoc.ConvertError as exc:
    print(f"convert: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)
except OSError as exc:
    print(f"convert: {exc}", file=sys.stderr)
    sys.exit(1)
"""


def venv_python() -> Path:
    override = os.environ.get("ANON_CONVERTER_PYTHON")
    if override:
        return Path(override).expanduser()
    return DEFAULT_VENV / "bin" / "python"


def has_anydoc(python: str) -> bool:
    try:
        return subprocess.run([python, "-c", "import anydoc"], capture_output=True).returncode == 0
    except OSError:
        return False


def ensure_venv() -> bool:
    python = venv_python()
    if has_anydoc(str(python)):
        return True
    print(f"convert: creating a pinned venv in {DEFAULT_VENV} (first use)…", file=sys.stderr)
    import venv

    venv.create(DEFAULT_VENV, with_pip=True)
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=False)
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", PIN], check=True)
    return has_anydoc(str(python))


def convert(path: Path) -> int:
    if not path.is_file():
        print(f"convert: not a file: {path}", file=sys.stderr)
        return 2
    # 1) the running interpreter (the container case)
    try:
        import anydoc  # noqa: F401

        result = subprocess.run([sys.executable, "-c", _CONVERT_CODE, str(path)], check=False)
        return result.returncode
    except ImportError:
        pass
    # 2) the pinned venv
    python = str(venv_python())
    if not has_anydoc(python) and not ensure_venv():
        print(
            "convert: no converter engine available. Install it with "
            f"`pip install {PIN}` or run `convert.py --install`.",
            file=sys.stderr,
        )
        return 2
    return subprocess.run([python, "-c", _CONVERT_CODE, str(path)], check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="convert.py", description="document → Markdown, locally")
    parser.add_argument("file", nargs="?", help="document to convert")
    parser.add_argument("--doctor", action="store_true", help="report which engine would be used")
    parser.add_argument("--install", action="store_true", help="provision the pinned venv")
    args = parser.parse_args(argv)

    if args.doctor:
        try:
            import anydoc  # noqa: F401

            print(f"convert: anydoc is importable in {sys.executable}")
            return 0
        except ImportError:
            python = str(venv_python())
            ok = has_anydoc(python)
            print(f"convert: venv {venv_python()} usable={ok}")
            return 0 if ok else 2
    if args.install:
        return 0 if ensure_venv() else 2
    if not args.file:
        parser.error("a file is required (or --doctor)")
    return convert(Path(args.file).expanduser())


if __name__ == "__main__":
    sys.exit(main())
