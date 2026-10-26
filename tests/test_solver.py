"""Solver tests, with the pruning cross-checked against the plain search.

Conflict-directed backjumping is the one place where a wrong blame set would
turn an unsatisfiable file into a satisfiable one without any visible error,
so the two modes are compared verdict for verdict on random universes.
"""

from __future__ import annotations

import random

import pytest

from pinmend.solver import Resolver, RootReq
from tests.conftest import make_index, random_index, random_roots


def roots_of(lines):
    return [RootReq.parse(i, line) for i, line in enumerate(lines)]


def solve(index, lines, backjump=True):
    roots = roots_of(lines)
    resolver = Resolver(index, roots, backjump=backjump)
    return resolver.solve(frozenset(r.rid for r in roots))


def test_simple_chain_resolves():
    index = make_index({
        "app": {"1.0": ["lib>=2.0"]},
        "lib": {"1.0": [], "2.0": [], "3.0": []},
    })
    result = solve(index, ["app==1.0"])
    assert result.sat
    assert str(result.assignment["lib"]) == "3.0"


def test_direct_pin_conflict_is_detected():
    index = make_index({
        "app": {"1.0": ["lib<2.0"]},
        "lib": {"1.0": [], "2.0": []},
    })
    result = solve(index, ["app==1.0", "lib==2.0"])
    assert not result.sat
    assert result.cause_union == frozenset({0, 1})


def test_backtracking_finds_the_one_working_combination():
    index = make_index({
        "app": {"1.0": ["lib<2.0"], "2.0": ["lib>=2.0"]},
        "lib": {"1.0": [], "2.0": []},
    })
    result = solve(index, ["app", "lib==1.0"])
    assert result.sat
    assert str(result.assignment["app"]) == "1.0"


def test_relaxed_line_keeps_the_package_installed():
    index = make_index({
        "app": {"1.0": ["lib<2.0"], "2.0": ["lib>=2.0"]},
        "lib": {"1.0": [], "2.0": []},
    })
    roots = roots_of(["app==2.0", "lib==1.0"])
    resolver = Resolver(index, roots)
    assert not resolver.solve(frozenset({0, 1})).sat
    relaxed = resolver.solve(frozenset({1}))
    assert relaxed.sat
    assert "app" in relaxed.assignment
    assert str(relaxed.assignment["app"]) == "1.0"


def test_unsatisfiability_is_monotone_in_the_enabled_lines():
    index = make_index({
        "app": {"1.0": ["lib<2.0"], "2.0": ["lib>=2.0"]},
        "lib": {"1.0": [], "2.0": []},
    })
    roots = roots_of(["app==2.0", "lib==1.0"])
    resolver = Resolver(index, roots)
    assert resolver.solve(frozenset()).sat
    assert resolver.solve(frozenset({0})).sat
    assert resolver.solve(frozenset({1})).sat
    assert not resolver.solve(frozenset({0, 1})).sat


@pytest.mark.parametrize("seed", range(120))
def test_backjumping_never_changes_the_verdict(seed):
    rng = random.Random(seed)
    index = random_index(rng, packages=rng.randint(4, 7), versions=rng.randint(2, 4))
    lines = random_roots(rng, index, rng.randint(2, 5))
    fast = solve(index, lines, backjump=True)
    plain = solve(index, lines, backjump=False)
    assert fast.sat == plain.sat, (lines, fast.sat, plain.sat)


@pytest.mark.parametrize("seed", range(60))
def test_reported_plan_satisfies_every_constraint(seed):
    rng = random.Random(500 + seed)
    index = random_index(rng, packages=rng.randint(4, 7), versions=rng.randint(2, 4))
    lines = random_roots(rng, index, rng.randint(2, 5))
    result = solve(index, lines)
    if not result.sat:
        pytest.skip("unsatisfiable instance")
    roots = roots_of(lines)
    for root in roots:
        chosen = result.assignment[root.name]
        assert root.specifier.contains(chosen, prereleases=True)
    for name, version in result.assignment.items():
        release = next(r for r in index.packages[name].releases if r.version == version)
        for dep in release.deps_base:
            assert dep.name in result.assignment
            assert dep.specifier.contains(result.assignment[dep.name], prereleases=True)
