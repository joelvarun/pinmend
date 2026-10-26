"""Run every example file through pinmend and print a comparison table.

    python examples/demo.py

Nothing here needs network access: the package metadata snapshot in data/ is
committed, so the numbers are the same on any machine.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pinmend import (
    PackageIndex,
    best_greedy,
    minimum_repair,
    read_requirements,
    relax_all_blamed,
    report_blame,
)

HEADER = "%-24s %5s %7s %7s %7s %7s %6s" % (
    "file", "lines", "report", "trusted", "loop", "minimum", "cores",
)


def main() -> int:
    index = PackageIndex.load(ROOT / "pinmend" / "data" / "pypi_snapshot.json.gz")
    print("index: %d packages, %d releases, %d dependency names outside the snapshot"
          % (len(index.packages), index.release_count(), len(index.unknown_refs)))
    print()
    print(HEADER)
    print("-" * len(HEADER))

    totals = {"loop": 0, "minimum": 0, "trusted_ok": 0, "files": 0}
    started = time.perf_counter()
    for path in sorted((ROOT / "examples").glob("*.txt")):
        roots = read_requirements(str(path))
        blame = report_blame(index, roots)
        if blame.succeeded:
            print("%-24s %5d  installs as written" % (path.stem, len(roots)))
            continue
        trusted = relax_all_blamed(index, roots)
        loop = best_greedy(index, roots)
        exact = minimum_repair(index, roots)
        print("%-24s %5d %7d %7s %7d %7d %6d"
              % (path.stem, len(roots), blame.blame_size,
                 "works" if trusted.succeeded else "fails",
                 len(loop.relaxed), len(exact.relaxed), len(exact.cores)))
        totals["loop"] += len(loop.relaxed)
        totals["minimum"] += len(exact.relaxed)
        totals["trusted_ok"] += int(trusted.succeeded)
        totals["files"] += 1

    print("-" * len(HEADER))
    print("%-24s %5s %7s %7s %7d %7d" % (
        "total", "", "",
        "%d/%d" % (totals["trusted_ok"], totals["files"]),
        totals["loop"], totals["minimum"]))
    print()
    print("report   lines the conflict report names, which you would have to read")
    print("trusted  whether unpinning exactly those lines produces an installable file")
    print("loop     lines unpinned by relaxing one named line at a time until it installs")
    print("minimum  lines unpinned by pinmend, proved to be the fewest possible")
    print("cores    independent conflicts, one line of each must be unpinned")
    print()
    print("%d files in %.2fs" % (totals["files"], time.perf_counter() - started))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
