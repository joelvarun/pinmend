"""Measure every strategy on the same broken requirements files.

Part one is the seven hand-written example files in examples/, each a real pin
conflict taken from published package metadata. Part two is a seeded random
suite: pick three to six package clusters, pin a dozen to thirty of their
packages at versions sampled from real release history with a bias toward
older releases, and keep the files that do not resolve. That is what a stale
requirements file looks like.

Writes docs/results.json. bench/make_chart.py turns it into the figures.
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pinmend import (
    PackageIndex,
    RootReq,
    best_greedy,
    minimum_repair,
    read_requirements,
    relax_all_blamed,
    report_blame,
)
from pinmend.repair import pareto_front
from pinmend.solver import Resolver, ResolveBudgetExceeded, Unrepairable

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "pinmend" / "data" / "pypi_snapshot.json.gz"
OUT = ROOT / "docs" / "results.json"

CLUSTER_SEEDS = [
    "flask", "requests", "boto3", "scipy", "pandas", "scikit-learn", "matplotlib",
    "tensorflow", "torch", "fastapi", "uvicorn", "black", "jsonschema", "httpx",
]
SUITE_SIZE = 150
RANDOM_SEED = 7
# Pathological files are skipped rather than allowed to dominate the run. The
# count of skips is reported so the suite cannot quietly become easier.
NODE_BUDGET = 20_000


def cluster(index: PackageIndex, seed: str) -> list[str]:
    """Packages reachable from a seed inside the snapshot: one realistic file."""
    seen = {seed}
    stack = [seed]
    while stack:
        name = stack.pop()
        pkg = index.get(name)
        if pkg is None:
            continue
        for release in pkg.releases[-6:]:
            for dep in release.deps_base:
                if dep.name in index.packages and dep.name not in seen:
                    seen.add(dep.name)
                    stack.append(dep.name)
    return sorted(seen)


def measure(index: PackageIndex, roots: list[RootReq], budget: int = NODE_BUDGET) -> dict | None:
    everything = frozenset(r.rid for r in roots)
    probe = Resolver(index, roots, node_budget=budget).solve(everything)
    if probe.sat:
        return None
    exact = minimum_repair(index, roots, node_budget=budget, tie_break=True)
    arbitrary = minimum_repair(index, roots, node_budget=budget, tie_break=False)
    greedy = best_greedy(index, roots, node_budget=budget)
    blame = report_blame(index, roots, node_budget=budget)
    trust = relax_all_blamed(index, roots, node_budget=budget)
    return {
        "lines": len(roots),
        "report_named": blame.blame_size,
        "trust_report_works": trust.succeeded,
        "trust_report_size": len(trust.relaxed),
        "greedy_size": len(greedy.relaxed),
        "greedy_rounds": len(greedy.relaxed),
        "greedy_ok": greedy.succeeded,
        "greedy_steps": greedy.steps_moved,
        "minimum_size": len(exact.relaxed),
        "minimum_steps": exact.steps_moved,
        "arbitrary_minimum_steps": arbitrary.steps_moved,
        "cores": len(exact.cores),
        "oracle_calls": exact.oracle_calls,
    }


def run_examples(index: PackageIndex) -> list[dict]:
    rows = []
    for path in sorted((ROOT / "examples").glob("*.txt")):
        roots = read_requirements(str(path))
        row = measure(index, roots, budget=200_000)
        if row is None:
            continue
        row["name"] = path.stem
        row["relax"] = None
        exact = minimum_repair(index, roots)
        row["relax"] = [roots[i].line for i in sorted(exact.relaxed)]
        row["pareto"] = pareto_front(index, roots, exact.cores, max_extra=2)
        rows.append(row)
    return rows


def run_suite(index: PackageIndex) -> dict:
    rng = random.Random(RANDOM_SEED)
    pools = {s: cluster(index, s) for s in CLUSTER_SEEDS}
    rows: list[dict] = []
    attempts = 0
    skipped = 0
    started = time.perf_counter()
    while len(rows) < SUITE_SIZE and attempts < SUITE_SIZE * 12:
        attempts += 1
        picked = rng.sample(CLUSTER_SEEDS, rng.randint(3, 6))
        pool = sorted(set().union(*(pools[s] for s in picked)))
        count = min(len(pool), rng.randint(12, 30))
        lines = []
        for name in rng.sample(pool, count):
            releases = index.get(name).releases
            idx = int(len(releases) * rng.betavariate(2.0, 2.2))
            idx = min(max(idx, 0), len(releases) - 1)
            lines.append(f"{index.get(name).project}=={releases[idx].raw}")
        roots = [RootReq.parse(i, line) for i, line in enumerate(lines)]
        try:
            row = measure(index, roots)
        except (ResolveBudgetExceeded, RecursionError, Unrepairable):
            skipped += 1
            continue
        if row is not None:
            rows.append(row)
            if len(rows) % 25 == 0:
                print("  suite %d/%d files (%.0fs)"
                      % (len(rows), SUITE_SIZE, time.perf_counter() - started), flush=True)
    return {
        "rows": rows,
        "attempts": attempts,
        "skipped": skipped,
        "seconds": round(time.perf_counter() - started, 2),
    }


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    mean = lambda key: sum(r[key] for r in rows) / n
    worse = [r for r in rows if r["greedy_ok"] and r["greedy_size"] > r["minimum_size"]]
    over = [r for r in rows if r["report_named"] > r["minimum_size"]]
    under = [r for r in rows if r["report_named"] < r["minimum_size"]]
    tie_gain = [r for r in rows if r["arbitrary_minimum_steps"] > r["minimum_steps"]]
    return {
        "files": n,
        "mean_lines_per_file": round(mean("lines"), 2),
        "mean_report_named": round(mean("report_named"), 2),
        "mean_greedy_size": round(mean("greedy_size"), 2),
        "mean_minimum_size": round(mean("minimum_size"), 2),
        "total_greedy_size": sum(r["greedy_size"] for r in rows),
        "total_minimum_size": sum(r["minimum_size"] for r in rows),
        "greedy_rounds_mean": round(mean("greedy_rounds"), 2),
        "greedy_suboptimal_files": len(worse),
        "greedy_suboptimal_pct": round(100 * len(worse) / n, 1),
        "greedy_mean_excess_when_worse": round(
            sum(r["greedy_size"] - r["minimum_size"] for r in worse) / len(worse), 2
        ) if worse else 0.0,
        "greedy_max_excess": max((r["greedy_size"] - r["minimum_size"] for r in worse), default=0),
        "trust_report_success_pct": round(
            100 * sum(1 for r in rows if r["trust_report_works"]) / n, 1
        ),
        "report_overstates_pct": round(100 * len(over) / n, 1),
        "report_understates_pct": round(100 * len(under) / n, 1),
        "report_exact_pct": round(100 * (n - len(over) - len(under)) / n, 1),
        "mean_cores": round(mean("cores"), 2),
        "mean_oracle_calls": round(mean("oracle_calls"), 2),
        "mean_steps_arbitrary_minimum": round(mean("arbitrary_minimum_steps"), 2),
        "mean_steps_best_minimum": round(mean("minimum_steps"), 2),
        "tie_break_helps_files": len(tie_gain),
        "size_histogram": {
            str(k): sum(1 for r in rows if r["minimum_size"] == k)
            for k in sorted({r["minimum_size"] for r in rows})
        },
    }


def main() -> int:
    started = time.perf_counter()
    index = PackageIndex.load(INDEX)
    print("index: %d packages, %d releases, %d unmodelled dependency names, snapshot %s"
          % (len(index.packages), index.release_count(), len(index.unknown_refs),
             index.meta.get("fetched_utc")), flush=True)

    examples = run_examples(index)
    print("examples: %d broken files" % len(examples))
    for row in examples:
        print("  %-24s lines=%-3d report_named=%-2d greedy=%-2d minimum=%-2d cores=%-2d"
              % (row["name"], row["lines"], row["report_named"],
                 row["greedy_size"], row["minimum_size"], row["cores"]))

    suite = run_suite(index)
    stats = summarise(suite["rows"])
    print("suite: %d files from %d attempts in %.1fs (%d skipped)"
          % (stats["files"], suite["attempts"], suite["seconds"], suite["skipped"]))
    for key in ("mean_lines_per_file", "mean_report_named", "mean_greedy_size",
                "mean_minimum_size", "greedy_suboptimal_pct", "greedy_mean_excess_when_worse",
                "trust_report_success_pct", "report_understates_pct", "report_overstates_pct",
                "mean_cores", "mean_oracle_calls", "mean_steps_arbitrary_minimum",
                "mean_steps_best_minimum"):
        print("  %-32s %s" % (key, stats[key]))

    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index": {
            "packages": len(index.packages),
            "releases": index.release_count(),
            "unmodelled_dependency_names": len(index.unknown_refs),
            "fetched_utc": index.meta.get("fetched_utc"),
            "target_python": "3.10",
        },
        "random_seed": RANDOM_SEED,
        "examples": examples,
        "suite": {k: v for k, v in suite.items() if k != "rows"},
        "suite_rows": suite["rows"],
        "summary": stats,
        "total_seconds": round(time.perf_counter() - started, 2),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print("wrote %s in %.1fs total" % (OUT, payload["total_seconds"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
