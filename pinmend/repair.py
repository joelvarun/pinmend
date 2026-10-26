"""Minimum-cardinality repair of an unsatisfiable requirements file.

Formally: the index's own dependency edges are hard constraints, each line of
the requirements file is a soft constraint, and a repair is a set of lines to
relax that makes the whole thing satisfiable (a correction subset). A set is a
correction subset exactly when it intersects every minimal unsatisfiable subset
(MUS), so the minimum repair is the minimum hitting set over all MUSes. This
module computes it with the implicit hitting set loop: alternate between an
exact hitting set over the MUSes found so far and one resolver call, adding a
new MUS whenever the candidate still fails.

Optimality: let H be the returned set and G any correction subset. G hits every
MUS, in particular every collected core, so the size of G is at least the size
of the minimum hitting set of the collected cores, which is the size of H. H
itself was satisfiability-checked, so it is a correction subset. Therefore H is
of minimum cardinality, and no enumeration of all MUSes was needed to prove it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packaging.version import Version

from .hitting import min_hitting_set, minimal_hitting_sets, pad_to_size
from .index import PackageIndex
from .solver import Resolver, RootReq, Solution, Unrepairable


@dataclass
class Repair:
    relaxed: frozenset[int]
    assignment: dict[str, Version]
    steps_moved: int
    cores: list[frozenset[int]] = field(default_factory=list)
    oracle_calls: int = 0
    resolver_nodes: int = 0
    proven_minimal: bool = True


def pin_anchors(index: PackageIndex, roots: list[RootReq]) -> dict[str, Version]:
    """Exact pins become version preferences, so a relaxed line still lands
    near what the user asked for and the secondary metric is well defined."""
    anchors: dict[str, Version] = {}
    for root in roots:
        pinned = [s.version for s in root.specifier if s.operator in ("==", "===")]
        if len(pinned) == 1 and index.get(root.name):
            try:
                anchors[root.name] = Version(pinned[0])
            except Exception:
                continue
    return anchors


def steps_moved(index: PackageIndex, anchors: dict[str, Version], assignment) -> int:
    """Release steps between each pinned version and the one actually chosen."""
    total = 0
    for name, pinned in anchors.items():
        chosen = assignment.get(name)
        pkg = index.get(name)
        if chosen is None or pkg is None:
            continue
        a, b = pkg.rank(pinned), pkg.rank(chosen)
        if a >= 0 and b >= 0:
            total += abs(a - b)
    return total


def extract_mus(resolver: Resolver, unsat: frozenset[int], seed: frozenset[int]) -> frozenset[int]:
    """Deletion-based minimal unsatisfiable subset.

    The solver's cause tracking supplies a seed, but a seed is only a
    heuristic, so it is satisfiability-checked before use and discarded if it
    does not reproduce the failure. The deletion pass then makes the result
    minimal regardless of where it started.
    """
    work = set(unsat)
    if seed and seed < unsat and not resolver.solve(seed).sat:
        work = set(seed)
    for element in sorted(unsat):
        if element not in work:
            continue
        trial = frozenset(work - {element})
        if not resolver.solve(trial).sat:
            work = set(trial)
    return frozenset(work)


def minimum_repair(
    index: PackageIndex,
    roots: list[RootReq],
    node_budget: int = 200_000,
    tie_break: bool = True,
) -> Repair:
    """Smallest set of requirement lines whose relaxation makes the file install.

    With tie_break on, the result is additionally the minimum-cardinality
    repair that moves the fewest releases away from the versions the file
    asked for. Equally small repairs are not equally good: on the worked
    example in the README two four-line repairs differ by nine release steps.
    The tie-break is exact because every correction subset hits every core, so
    enumerating the size-k hitting sets of the collected cores and checking
    each one cannot miss a repair of that size.
    """
    anchors = pin_anchors(index, roots)
    resolver = Resolver(index, roots, anchors=anchors, node_budget=node_budget)
    everything = frozenset(r.rid for r in roots)

    baseline: Solution = resolver.solve(everything)
    if baseline.sat:
        return Repair(
            relaxed=frozenset(),
            assignment=baseline.assignment,
            steps_moved=steps_moved(index, anchors, baseline.assignment),
            oracle_calls=resolver.calls,
            resolver_nodes=resolver.total_nodes,
        )

    cores: list[frozenset[int]] = []
    while True:
        candidate = min_hitting_set(cores)
        enabled = everything - candidate
        attempt = resolver.solve(enabled)
        if attempt.sat:
            best = (candidate, attempt.assignment,
                    steps_moved(index, anchors, attempt.assignment))
            if tie_break:
                size = len(candidate)
                for other in minimal_hitting_sets(cores, size):
                    if len(other) != size or other == candidate:
                        continue
                    trial = resolver.solve(everything - other)
                    if not trial.sat:
                        continue
                    moved = steps_moved(index, anchors, trial.assignment)
                    if moved < best[2]:
                        best = (other, trial.assignment, moved)
            return Repair(
                relaxed=best[0],
                assignment=best[1],
                steps_moved=best[2],
                cores=cores,
                oracle_calls=resolver.calls,
                resolver_nodes=resolver.total_nodes,
            )
        seed = attempt.cause_union & enabled
        core = extract_mus(resolver, enabled, seed)
        if not core:
            raise Unrepairable(
                "unpinning every line still leaves no installable combination"
            )
        cores.append(core)


def pareto_front(
    index: PackageIndex,
    roots: list[RootReq],
    cores: list[frozenset[int]],
    max_extra: int = 3,
    node_budget: int = 200_000,
) -> list[dict]:
    """Trade-off curve: for each repair size, the least release movement.

    Its first point is the provably minimum-cardinality repair; the later
    points answer what unpinning one more line buys. Enumeration is exact
    whenever the exhaustive flag on a point is true: every correction subset
    hits every core, so filtering candidates by the core condition before the
    satisfiability check cannot discard a valid repair. On files where the
    padded candidate set would exceed the cap the flag is false and that point
    is a bound rather than the optimum.
    """
    anchors = pin_anchors(index, roots)
    resolver = Resolver(index, roots, anchors=anchors, node_budget=node_budget)
    everything = frozenset(r.rid for r in roots)
    smallest = len(min_hitting_set(cores))
    front: list[dict] = []
    for size in range(smallest, min(smallest + max_extra, len(everything)) + 1):
        candidates, complete = pad_to_size(cores, sorted(everything), size)
        best: dict | None = None
        for candidate in candidates:
            attempt = resolver.solve(everything - candidate)
            if not attempt.sat:
                continue
            moved = steps_moved(index, anchors, attempt.assignment)
            if best is None or moved < best["steps_moved"]:
                best = {
                    "size": size,
                    "relaxed": sorted(candidate),
                    "steps_moved": moved,
                    "exhaustive": complete,
                }
        if best is not None:
            front.append(best)
    return front
