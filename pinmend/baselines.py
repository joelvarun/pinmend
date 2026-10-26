"""What the existing tools give you, implemented against the same resolver.

Three reference strategies, all reading the same index and the same requirement
lines as the exact repair, so any difference in the numbers comes from the
method and not from the data:

report_blame      the conflict report a resolver prints on failure, reduced to
                  the user's own requirement lines (the inspection burden)
relax_all_blamed  relax every line named in that report, in one shot
greedy_iterative  relax one named line, resolve again, repeat until it installs
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .index import PackageIndex
from .repair import pin_anchors, steps_moved
from .solver import Conflict, Resolver, RootReq


@dataclass
class BaselineResult:
    name: str
    relaxed: frozenset[int]
    resolver_calls: int
    steps_moved: int
    succeeded: bool
    blame_size: int = 0
    trace: list[int] = field(default_factory=list)


def format_blame(roots: list[RootReq], conflict: Conflict) -> str:
    """Render a conflict the way a resolver reports one, for the README."""
    lines = [f"cannot satisfy {conflict.package}, no release matches every constraint:"]
    for spec, cause in conflict.constraints:
        if not cause:
            continue
        if len(cause) == 1 and roots[next(iter(cause))].name == conflict.package:
            lines.append(f"    the file requested {roots[next(iter(cause))].line}")
        else:
            via = ", ".join(roots[c].line for c in sorted(cause))
            lines.append(f"    {conflict.package}{spec} required via {via}")
    return "\n".join(lines)


def report_blame(index: PackageIndex, roots: list[RootReq], node_budget: int = 200_000) -> BaselineResult:
    resolver = Resolver(index, roots, anchors=pin_anchors(index, roots), node_budget=node_budget)
    everything = frozenset(r.rid for r in roots)
    solution = resolver.solve(everything)
    blamed = solution.first_conflict.roots if solution.first_conflict else frozenset()
    return BaselineResult(
        name="conflict report",
        relaxed=frozenset(),
        resolver_calls=resolver.calls,
        steps_moved=0,
        succeeded=solution.sat,
        blame_size=len(blamed),
        trace=sorted(blamed),
    )


def relax_all_blamed(index: PackageIndex, roots: list[RootReq], node_budget: int = 200_000) -> BaselineResult:
    anchors = pin_anchors(index, roots)
    resolver = Resolver(index, roots, anchors=anchors, node_budget=node_budget)
    everything = frozenset(r.rid for r in roots)
    solution = resolver.solve(everything)
    if solution.sat:
        return BaselineResult("relax all blamed", frozenset(), resolver.calls, 0, True)
    blamed = solution.first_conflict.roots if solution.first_conflict else frozenset()
    attempt = resolver.solve(everything - blamed)
    return BaselineResult(
        name="relax all blamed",
        relaxed=blamed,
        resolver_calls=resolver.calls,
        steps_moved=steps_moved(index, anchors, attempt.assignment) if attempt.sat else 0,
        succeeded=attempt.sat,
        blame_size=len(blamed),
        trace=sorted(blamed),
    )


def greedy_iterative(index: PackageIndex, roots: list[RootReq], prefer_last: bool = False,
                     node_budget: int = 200_000) -> BaselineResult:
    """Relax one blamed line per round and resolve again.

    This is the hand loop the current tools leave you with: the resolver names
    some requirements, you unpin one of them, you run it again. Both tie-break
    directions are offered because which named line you pick is arbitrary, and
    the benchmark reports whichever direction does better.
    """
    anchors = pin_anchors(index, roots)
    resolver = Resolver(index, roots, anchors=anchors, node_budget=node_budget)
    everything = frozenset(r.rid for r in roots)
    relaxed: set[int] = set()
    order: list[int] = []
    while True:
        attempt = resolver.solve(everything - frozenset(relaxed))
        if attempt.sat:
            return BaselineResult(
                name="greedy, blame driven",
                relaxed=frozenset(relaxed),
                resolver_calls=resolver.calls,
                steps_moved=steps_moved(index, anchors, attempt.assignment),
                succeeded=True,
                trace=order,
            )
        blamed = set()
        if attempt.first_conflict:
            blamed = set(attempt.first_conflict.roots) - relaxed
        if not blamed:
            blamed = set(attempt.cause_union) - relaxed
        if not blamed:
            return BaselineResult(
                name="greedy, blame driven",
                relaxed=frozenset(relaxed),
                resolver_calls=resolver.calls,
                steps_moved=0,
                succeeded=False,
                trace=order,
            )
        pick = max(blamed) if prefer_last else min(blamed)
        relaxed.add(pick)
        order.append(pick)


def best_greedy(index: PackageIndex, roots: list[RootReq], node_budget: int = 200_000) -> BaselineResult:
    """Give the greedy baseline its best shot: run both tie-breaks and keep the
    smaller repair, so the comparison is against the better of the two
    arbitrary choices rather than against the unlucky one."""
    first = greedy_iterative(index, roots, prefer_last=False, node_budget=node_budget)
    last = greedy_iterative(index, roots, prefer_last=True, node_budget=node_budget)
    options = [r for r in (first, last) if r.succeeded] or [first]
    winner = min(options, key=lambda r: (len(r.relaxed), r.steps_moved))
    winner.resolver_calls = min(first.resolver_calls, last.resolver_calls)
    return winner
