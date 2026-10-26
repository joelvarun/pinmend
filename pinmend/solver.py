"""Complete backtracking version solver with cause-tracked conflicts.

Two things make this solver useful as a repair oracle rather than just an
installer:

1. Every derived constraint carries the set of requirement line ids that caused
   it to exist, so a failed resolve can name the lines in the file.
2. A conflict also carries the set of packages whose version choice could
   possibly remove it, which lets the search abandon whole subtrees whose
   decisions are irrelevant to the failure (conflict-directed backjumping).

Backjumping can be turned off, and tests/test_solver.py cross-checks the two
modes on randomly generated indexes so the pruning cannot silently change a
verdict.

Derived state (active constraints, active extras) is recomputed from the
current assignment at every node instead of being maintained on a trail. The
resolutions here touch tens of packages, so recomputation is cheap and it
removes a whole class of backtracking bugs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from .index import Dep, PackageIndex, Release


class ResolveBudgetExceeded(RuntimeError):
    pass


class Unrepairable(RuntimeError):
    """No set of relaxations helps: some package pair has no compatible pair of
    releases at all, so a line has to be deleted rather than unpinned."""


@dataclass(frozen=True)
class RootReq:
    rid: int
    line: str
    name: str
    specifier: SpecifierSet
    extras: frozenset[str]
    spec_text: str = ""

    @classmethod
    def parse(cls, rid: int, line: str) -> "RootReq":
        req = Requirement(line)
        return cls(
            rid=rid,
            line=line,
            name=canonicalize_name(req.name),
            specifier=req.specifier,
            extras=frozenset(canonicalize_name(e) for e in req.extras),
            spec_text=str(req.specifier),
        )


@dataclass
class Conflict:
    package: str
    constraints: tuple[tuple[str, frozenset[int]], ...]
    roots: frozenset[int]


@dataclass
class Solution:
    sat: bool
    assignment: dict[str, Version] = field(default_factory=dict)
    cause_union: frozenset[int] = frozenset()
    first_conflict: Conflict | None = None
    nodes: int = 0


def read_requirements(path: str) -> list[RootReq]:
    roots: list[RootReq] = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if line:
                roots.append(RootReq.parse(len(roots), line))
    return roots


class Resolver:
    """Oracle over one package index and one list of requirement lines."""

    def __init__(
        self,
        index: PackageIndex,
        roots: list[RootReq],
        anchors: dict[str, Version] | None = None,
        node_budget: int = 200_000,
        backjump: bool = True,
    ):
        self.index = index
        self.roots = roots
        self.anchors = anchors or {}
        self.node_budget = node_budget
        self.backjump = backjump
        self.calls = 0
        self.total_nodes = 0

    # -- derived state ---------------------------------------------------

    def _derive(self, enabled: frozenset[int], assigned: dict[str, Release]):
        """Constraints implied by the enabled lines plus the current choices.

        Each entry is (specifier, causing line ids, producing package or None).
        """
        constraints: dict[str, list[tuple[str, frozenset[int], str | None]]] = {}
        extras: dict[str, frozenset[str]] = {}

        def add(name, spec, ext, cause, src):
            constraints.setdefault(name, []).append((spec, cause, src))
            if ext:
                extras[name] = extras.get(name, frozenset()) | ext

        # Relaxing a line unpins it, it does not uninstall the package: the
        # package stays required, only its specifier is dropped. That keeps
        # unsatisfiability monotone in the enabled set, which is what the
        # minimal-unsatisfiable-subset argument in repair.py relies on.
        for root in self.roots:
            if root.name not in self.index.packages:
                continue
            if root.rid in enabled:
                add(root.name, root.spec_text, root.extras, frozenset({root.rid}), None)
            else:
                add(root.name, "", root.extras, frozenset(), None)

        seen_edges: set[tuple[str, Dep]] = set()
        changed = True
        while changed:
            changed = False
            for name in list(constraints):
                release = assigned.get(name)
                if release is None:
                    continue
                cause = frozenset().union(*(c for _, c, _ in constraints[name]))
                for dep in self.index.deps_of(name, release, extras.get(name, frozenset())):
                    key = (name, dep)
                    if key in seen_edges:
                        continue
                    seen_edges.add(key)
                    add(dep.name, dep.spec_text, dep.extras, cause, name)
                    changed = True
        return constraints, extras

    @staticmethod
    def _blame_packages(constraints, start: str) -> frozenset[str]:
        """Packages whose version choice could remove a conflict at start.

        Only the package itself and, transitively, the packages that produced
        the constraints on it, can change which releases remain available.
        """
        blame = {start}
        frontier = [start]
        while frontier:
            name = frontier.pop()
            for _, _, src in constraints.get(name, ()):
                if src is not None and src not in blame:
                    blame.add(src)
                    frontier.append(src)
        return frozenset(blame)

    def _candidate_mask(self, name: str, specs) -> int:
        bits = self.index.full_mask(name)
        for spec_text, _, _ in specs:
            if not spec_text:
                continue
            bits &= self.index.mask(name, spec_text)
            if not bits:
                break
        return bits

    def _order(self, name: str, cands: list[Release]) -> list[Release]:
        anchor = self.anchors.get(name)
        if anchor is None:
            return sorted(cands, key=lambda r: r.version, reverse=True)
        pkg = self.index.packages[name]
        target = pkg.rank(anchor)
        if target < 0:
            return sorted(cands, key=lambda r: r.version, reverse=True)
        return sorted(cands, key=lambda r: (abs(r.idx - target), -r.idx))

    # -- search ----------------------------------------------------------

    def solve(self, enabled: frozenset[int]) -> Solution:
        self.calls += 1
        state = _SearchState(enabled)
        result = self._search({}, state)
        self.total_nodes += state.nodes
        if isinstance(result, dict):
            return Solution(True, {n: r.version for n, r in result.items()},
                            frozenset(), None, state.nodes)
        return Solution(False, {}, frozenset(state.cause_union), state.first_conflict, state.nodes)

    def _search(self, assigned: dict[str, Release], state: "_SearchState"):
        """Return the assignment on success, or the blame package set on failure."""
        state.nodes += 1
        if state.nodes > self.node_budget:
            raise ResolveBudgetExceeded(f"node budget {self.node_budget} exhausted")

        constraints, _ = self._derive(state.enabled, assigned)
        cand_map: dict[str, int] = {}
        for name, specs in constraints.items():
            bits = self._candidate_mask(name, specs)
            chosen = assigned.get(name)
            if chosen is not None:
                if not (bits >> chosen.idx) & 1:
                    bits = 0
            if not bits:
                state.record(name, specs)
                return self._blame_packages(constraints, name)
            cand_map[name] = bits

        pending = [n for n in cand_map if n not in assigned]
        if not pending:
            return assigned

        name = min(pending, key=lambda n: (cand_map[n].bit_count(), -len(constraints[n]), n))
        blames: set[str] = set()
        for release in self._order(name, self.index.decode(name, cand_map[name])):
            found = self._search({**assigned, name: release}, state)
            if isinstance(found, dict):
                return found
            if self.backjump and name not in found:
                # No release of this package is implicated, so every remaining
                # candidate fails the same way. Hand the reason upward.
                return found
            blames |= found
        return frozenset(blames) | self._blame_packages(constraints, name)


class _SearchState:
    def __init__(self, enabled: frozenset[int]):
        self.enabled = enabled
        self.nodes = 0
        self.cause_union: set[int] = set()
        self.first_conflict: Conflict | None = None

    def record(self, package: str, specs) -> None:
        roots = frozenset().union(*(c for _, c, _ in specs)) if specs else frozenset()
        self.cause_union |= roots
        if self.first_conflict is None:
            self.first_conflict = Conflict(
                package=package,
                constraints=tuple((spec or "any", cause) for spec, cause, _ in specs),
                roots=roots,
            )
