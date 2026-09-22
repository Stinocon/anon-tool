#!/usr/bin/env python3
"""tag-collision.py — how wide must the per-map tag be? Measured, not argued.

Every placeholder carries the tag of the map that produced it (`[EMAIL-1-a3f9]`): that tag is what
makes a wrong map fail LOUDLY instead of substituting another client's values. If two live maps
shared a tag, the wrong map would resolve the placeholders silently — the exact failure the tag
exists to prevent. So "6 hex is negligible" deserves a number.

Two questions, two measurements:

  1. the RAW generator (`os.urandom(3).hex()[:6]`): how likely is a collision among N maps, and at
     which N does the first one arrive?
  2. the ALLOCATOR (`anon.new_tag(existing_tags(dir))`): is the guarantee exact by construction?

The simulation is seeded, so the numbers below reproduce on any machine; the real generator is
`os.urandom`, which is why the figure to trust is the analytic curve, not a single run.

    python3 scripts/tag-collision.py
"""

from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import anon  # noqa: E402  (the engine under test)

SPACE = 16**anon.TAG_DIGITS
TRIALS = 200


def birthday(n: int, space: int = SPACE) -> float:
    """P(at least one collision among n draws from `space` uniform values)."""
    return 1.0 - math.exp(-n * (n - 1) / (2.0 * space))


def first_collision_point(space: int = SPACE) -> int:
    """The n where the EXPECTED number of colliding pairs reaches 1 (the ~50% mark is lower)."""
    return int(math.isqrt(2 * space))


def observed(n: int, trials: int = TRIALS) -> int:
    """How many of `trials` simulated runs of n maps had at least one collision."""
    hits = 0
    for seed in range(trials):
        rng = random.Random(seed)
        seen = {rng.randrange(SPACE) for _ in range(n)}
        if len(seen) < n:
            hits += 1
    return hits


def allocator_trial(n: int) -> int:
    """Collisions produced by the real allocator across n maps: must be zero."""
    taken: set[str] = set()
    duplicates = 0
    for _ in range(n):
        tag = anon.new_tag(taken)
        if tag in taken:
            duplicates += 1
        taken.add(tag)
    return duplicates


def main() -> int:
    print(f"tag width: {anon.TAG_DIGITS} hex digits -> {SPACE:,} values · {TRIALS} seeded runs\n")
    print(f"{'maps':>7} | {'observed':>10} | {'birthday':>10} | {'expected collisions':>19}")
    print("-" * 58)
    for n in (100, 1_000, 2_000, 5_000, 10_000, 50_000):
        hits = observed(n)
        print(f"{n:>7,} | {hits / TRIALS:>9.1%} | {birthday(n):>9.1%} | {n * (n - 1) / 2.0 / SPACE:>19.3f}")
    print(f"\nthe expected collision count reaches 1 at about {first_collision_point():,} maps")
    print("(the 50% mark is lower: the birthday bound, ~1.18*sqrt(space))\n")

    print("allocator (`new_tag(existing_tags(...))`), duplicates produced:")
    for n in (1_000, 5_000):
        print(f"  {n:>6,} maps -> {allocator_trial(n)} duplicate(s)")

    maps_dir = anon.DEFAULT_MAPS
    if maps_dir.is_dir():
        start = time.perf_counter()
        tags = anon.existing_tags(maps_dir)
        elapsed = (time.perf_counter() - start) * 1000
        print(f"\ncost on this machine: existing_tags({maps_dir}) read {len(tags)} tag(s) in {elapsed:.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
