#!/usr/bin/env python3
"""check-container-fresh.py — a running anon-tool container must serve the CURRENT code.

Why this is a gate and not a reminder: the image bakes `anon.py`, `deanon.py`, `suggest.py` and
`web/`, while the `~/.anon` volume carries only DATA. A fix committed and pushed is therefore
invisible in the browser until the container is rebuilt and restarted — and the version number does
not move between commits, so a stale container is indistinguishable from a current one on the page.
That is exactly how the English translation shipped "fixed" and still showed Italian: the code was
right, the container was old, and nothing on screen said so.

The check fingerprints the code a build ships — whatever `anon.code_fingerprint` covers, which is
the single source for that set — both in this repository and inside each running `anon-tool` /
`anon-tool-slim` container (the compose services; a hand-run `docker run` with a random name is out
of scope), and fails when they differ. Versions do not enter into it; a single edited byte changes
the digest.

    python3 scripts/check-container-fresh.py

Exit 0 when no anon-tool container is running (nothing to keep fresh), or when each running one
matches the repository. Exit 1 when one is stale, printing the command that fixes it. Exit 2 when
docker is installed but cannot be queried — an unusable daemon must not read as "fresh". A machine
that never installs docker is not blocked.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import anon  # noqa: E402

# The compose project names the two services; the suggestion-model sidecar shares the UI's network
# namespace and is not an anon-tool build, so it is not checked.
CONTAINERS = ("anon-tool", "anon-tool-slim")
# The command that rebuilds each service, kept next to the names so the remediation cannot name the
# wrong one (a stale slim container is not fixed by `make up MODEL=1`).
REBUILD = {"anon-tool": "make up MODEL=1", "anon-tool-slim": "make up-slim"}


def running_containers() -> tuple[list[str], str]:
    """`(names, error)`. An unusable daemon is an ERROR, not an empty list: collapsing the two would
    let a broken Docker install read as "nothing to keep fresh"."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        return [], message[-1] if message else f"docker ps exited {result.returncode}"
    return [name for name in CONTAINERS if name in result.stdout.split()], ""


def container_fingerprint(name: str) -> tuple[str | None, str]:
    """What the container reports about itself: `(fingerprint, error)`. `None` = it cannot say.

    A build that predates the fingerprint has no such command; that is not an error to hide but the
    loudest possible "stale", so the failure is returned rather than swallowed.
    """
    command = ["docker", "exec", "-w", "/app", name, "python3", "-c", "import anon; print(anon.code_fingerprint())"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        return None, message[-1] if message else "no output"
    return result.stdout.strip(), ""


def main() -> int:
    if shutil.which("docker") is None:
        print("container-fresh: docker is not installed — nothing to check")
        return 0

    names, error = running_containers()
    if error:
        print(f"container-fresh: cannot determine the running containers ({error})", file=sys.stderr)
        print("container-fresh: start the Docker daemon, or remove docker if this machine never runs it", file=sys.stderr)
        return 2
    if not names:
        print("container-fresh: no anon-tool container running — nothing to keep fresh")
        return 0

    stale: list[str] = []
    # The full image ships `convert.py`, the slim one does not. Rather than infer the variant from
    # the container NAME (which a hand-run container can get wrong), the build is accepted when it
    # matches the host's own digest computed either with or without `convert.py` — its self-report
    # already says which it has, and any real code drift still differs from both.
    expected = {
        anon.code_fingerprint(ROOT, with_convert=True),
        anon.code_fingerprint(ROOT, with_convert=False),
    }
    for name in names:
        actual, failure = container_fingerprint(name)
        if failure:
            print(f"container-fresh: {name} does not report a build ({failure}) — it predates this check")
            stale.append(name)
        elif actual not in expected:
            print(f"container-fresh: {name} is STALE — running {actual[:12]}, which matches no repository build")
            stale.append(name)
        else:
            print(f"container-fresh: {name} matches the repository ({actual[:12]})")

    if stale:
        commands = " / ".join(sorted({REBUILD.get(name, "make up MODEL=1") for name in stale}))
        print(f"container-fresh: rebuild and restart: {commands}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
