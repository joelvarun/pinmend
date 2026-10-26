"""Repair tests: the claimed minimum is compared against brute force."""

from __future__ import annotations

import random
from itertools import combinations

import pytest

from pinmend.repair import extract_mus, minimum_repair, pin_anchors
from pinmend.solver import Resolver, RootReq, Unrepairable
from tests.conftest import make_index, random_index, random_roots


def roots_of(lines):
    return [RootReq.parse(i, line) for i, line in enumerate(lines)]


def brute_force_minimum(index, roots) -> int | None:
    """Smallest number of lines whose relaxation makes the file resolve."""
    everything = frozenset(r.rid for r in roots)
    resolver = Resolver(index, roots)
    for size in range(len(roots) + 1):
        for combo in combinations(sorted(everything), size):
            if resolver.solve(everything - frozenset(combo)).sat:
                return size
    return None


@pytest.mark.parametrize("seed", range(90))
def test_minimum_repair_is_really_minimum(seed):
    rng = random.Random(seed)
    index = random_index(rng, packages=rng.randint(4, 7), versions=rng.randint(2, 4))
    lines = random_roots(rng, index, rng.randint(2, 6))
    roots = roots_of(lines)
    expected = brute_force_minimum(index, roots)
    try:
        repair = minimum_repair(index, roots)
    except Unrepairable:
        assert expected is None
        return
    assert expected is not None
    assert len(repair.relaxed) == expected, (lines, sorted(repair.relaxed), expected)


@pytest.mark.parametrize("seed", range(60))
def test_repair_actually_installs_and_keeps_every_package(seed):
    rng = random.Random(300 + seed)
    index = random_index(rng, packages=rng.randint(4, 7), versions=rng.randint(2, 4))
    lines = random_roots(rng, index, rng.randint(2, 6))
    roots = roots_of(lines)
    try:
        repair = minimum_repair(index, roots)
    except Unrepairable:
        pytest.skip("no relaxation helps")
    for root in roots:
        assert root.name in repair.assignment
        if root.rid not in repair.relaxed:
            assert root.specifier.contains(repair.assignment[root.name], prereleases=True)


@pytest.mark.parametrize("seed", range(40))
def test_every_core_is_minimal_and_unsatisfiable(seed):
    rng = random.Random(700 + seed)
    index = random_index(rng, packages=rng.randint(4, 7), versions=rng.randint(2, 4))
    lines = random_roots(rng, index, rng.randint(3, 6))
    roots = roots_of(lines)
    everything = frozenset(r.rid for r in roots)
    resolver = Resolver(index, roots)
    if resolver.solve(everything).sat:
        pytest.skip("satisfiable instance")
    try:
        repair = minimum_repair(index, roots)
    except Unrepairable:
        pytest.skip("no relaxation helps")
    for core in repair.cores:
        assert not resolver.solve(core).sat
        for element in core:
            assert resolver.solve(core - {element}).sat


def test_extract_mus_rejects_a_misleading_seed():
    """A seed that does not reproduce the failure must be discarded, not used."""
    index = make_index({
        "a": {"1.0": ["c<2.0"], "2.0": []},
        "b": {"1.0": []},
        "c": {"1.0": [], "2.0": []},
    })
    roots = roots_of(["a==1.0", "b==1.0", "c==2.0"])
    resolver = Resolver(index, roots)
    everything = frozenset({0, 1, 2})
    assert not resolver.solve(everything).sat
    core = extract_mus(resolver, everything, frozenset({1}))
    assert core == frozenset({0, 2})


def test_tie_break_picks_the_repair_that_moves_least():
    index = make_index({
        "web": {"1.0": ["core<2.0"], "2.0": ["core>=2.0"]},
        "core": {"1.0": [], "2.0": []},
        "tool": {"1.0": ["core<2.0"], "2.0": ["core>=2.0"]},
    })
    roots = roots_of(["web==2.0", "tool==1.0", "core==2.0"])
    anchors = pin_anchors(index, roots)
    assert set(anchors) == {"web", "tool", "core"}
    repair = minimum_repair(index, roots, tie_break=True)
    assert len(repair.relaxed) == 1
    assert repair.steps_moved <= minimum_repair(index, roots, tie_break=False).steps_moved


def test_unrepairable_is_reported_rather_than_looping():
    index = make_index({
        "left": {"1.0": ["shared<2.0"]},
        "right": {"1.0": ["shared>=2.0"]},
        "shared": {"1.0": [], "2.0": []},
    })
    roots = roots_of(["left==1.0", "right==1.0"])
    with pytest.raises(Unrepairable):
        minimum_repair(index, roots)
