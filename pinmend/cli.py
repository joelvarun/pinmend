"""Command line entry point: diagnose a requirements file and repair it."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .baselines import best_greedy, format_blame
from .index import PackageIndex
from .repair import minimum_repair, pareto_front, pin_anchors
from .solver import Resolver, read_requirements

DEFAULT_INDEX = Path(__file__).resolve().parent / "data" / "pypi_snapshot.json.gz"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pinmend",
        description="Find the provably smallest set of requirement lines to relax.",
    )
    parser.add_argument("requirements", help="path to a requirements file")
    parser.add_argument("--index", default=str(DEFAULT_INDEX), help="package metadata snapshot")
    parser.add_argument("--pareto", action="store_true", help="show the size versus movement curve")
    parser.add_argument("--compare", action="store_true", help="also run the blame driven loop")
    parser.add_argument("--any-minimum", action="store_true",
                        help="skip the release movement tie break between equal sized repairs")
    parser.add_argument("--json", action="store_true", help="emit machine readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    index = PackageIndex.load(args.index)
    roots = read_requirements(args.requirements)
    everything = frozenset(r.rid for r in roots)
    anchors = pin_anchors(index, roots)

    outside = [r.line for r in roots if index.get(r.name) is None]

    started = time.perf_counter()
    probe = Resolver(index, roots, anchors=anchors).solve(everything)
    out: dict = {
        "requirements": args.requirements,
        "lines": len(roots),
        "index": {
            "packages": len(index.packages),
            "releases": index.release_count(),
            "unmodelled_dependency_names": len(index.unknown_refs),
        },
        "installable": probe.sat,
        "lines_outside_the_snapshot": outside,
    }

    if probe.sat:
        out["plan"] = {n: str(v) for n, v in sorted(probe.assignment.items())}
        _emit(args, out, lambda: (_warn_outside(outside),
                                  _print_ok(roots, probe, time.perf_counter() - started)))
        return 0

    repair = minimum_repair(index, roots, tie_break=not args.any_minimum)
    out["conflict"] = {
        "package": probe.first_conflict.package if probe.first_conflict else None,
        "lines_named": sorted(probe.first_conflict.roots) if probe.first_conflict else [],
    }
    out["repair"] = {
        "relax": [roots[i].line for i in sorted(repair.relaxed)],
        "size": len(repair.relaxed),
        "release_steps_moved": repair.steps_moved,
        "cores": [sorted(c) for c in repair.cores],
        "oracle_calls": repair.oracle_calls,
        "proven_minimal": repair.proven_minimal,
    }
    out["plan"] = {n: str(v) for n, v in sorted(repair.assignment.items())}
    if args.compare:
        greedy = best_greedy(index, roots)
        out["blame_driven_loop"] = {
            "relax": [roots[i].line for i in sorted(greedy.relaxed)],
            "size": len(greedy.relaxed),
            "rounds": len(greedy.relaxed),
            "release_steps_moved": greedy.steps_moved,
        }
    if args.pareto:
        out["pareto"] = pareto_front(index, roots, repair.cores)
    out["seconds"] = round(time.perf_counter() - started, 3)
    _emit(args, out, lambda: (_warn_outside(outside),
                              _print_repair(roots, probe, repair, out)))
    return 1


def _warn_outside(outside: list[str]) -> None:
    if not outside:
        return
    print("%d line%s name a package the snapshot does not carry and %s ignored:"
          % (len(outside), "" if len(outside) == 1 else "s",
             "was" if len(outside) == 1 else "were"))
    for line in outside:
        print("    %s" % line)
    print()


def _emit(args, out: dict, printer) -> None:
    if args.json:
        json.dump(out, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        printer()


def _print_ok(roots, solution, seconds: float) -> None:
    print("%d requirement lines resolve, %d packages in the plan (%.3fs)"
          % (len(roots), len(solution.assignment), seconds))
    for name, version in sorted(solution.assignment.items()):
        print("    %s==%s" % (name, version))


def _print_repair(roots, probe, repair, out: dict) -> None:
    print("%d requirement lines, no satisfying set of releases exists." % len(roots))
    print()
    print("what a resolver reports:")
    if probe.first_conflict:
        for line in format_blame(roots, probe.first_conflict).splitlines():
            print("    " + line)
        named = sorted(probe.first_conflict.roots)
        print("    lines named by the report: %d" % len(named))
    print()
    print("minimum repair: unpin %d of %d lines (proved minimal over %d %s, %d resolver calls)"
          % (len(repair.relaxed), len(roots), len(repair.cores),
             "core" if len(repair.cores) == 1 else "cores", repair.oracle_calls))
    for rid in sorted(repair.relaxed):
        print("    - %s" % roots[rid].line)
    print("release steps moved away from the pinned versions: %d" % repair.steps_moved)
    print()
    if "blame_driven_loop" in out:
        loop = out["blame_driven_loop"]
        print("blame driven loop for comparison: %d lines over %d resolve round%s"
              % (loop["size"], loop["rounds"], "" if loop["rounds"] == 1 else "s"))
        for line in loop["relax"]:
            print("    - %s" % line)
        print()
    print("each unsatisfiable core, any one line of which must be relaxed:")
    for core in repair.cores:
        print("    {%s}" % ", ".join(roots[i].line for i in sorted(core)))
    if "pareto" in out:
        print()
        print("size versus movement, exact at every point:")
        print("    lines  release steps  relaxed")
        for point in out["pareto"]:
            print("    %5d  %13d  %s" % (point["size"], point["steps_moved"],
                                         ", ".join(roots[i].line for i in point["relaxed"])))
    print()
    print("resulting plan: %d packages" % len(out["plan"]))
    for name, version in sorted(out["plan"].items()):
        print("    %s==%s" % (name, version))


if __name__ == "__main__":
    raise SystemExit(main())
