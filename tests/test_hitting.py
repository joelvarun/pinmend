"""The hitting set code is the part that makes the answer provably minimal, so
it is checked against brute force rather than against itself."""

from __future__ import annotations

import random
from itertools import combinations

import pytest

from pinmend.hitting import (
    greedy_hitting_set,
    min_hitting_set,
    minimal_hitting_sets,
    prune_dominated,
)


def brute_force_minimum(cores, universe):
    for size in range(len(universe) + 1):
        for combo in combinations(sorted(universe), size):
            if all(set(combo) & core for core in cores):
                return size
    raise AssertionError("no hitting set exists")


@pytest.mark.parametrize("seed", range(60))
def test_min_hitting_set_matches_brute_force(seed):
    rng = random.Random(seed)
    universe = list(range(rng.randint(4, 12)))
    cores = [
        frozenset(rng.sample(universe, rng.randint(1, min(4, len(universe)))))
        for _ in range(rng.randint(1, 8))
    ]
    assert len(min_hitting_set(cores)) == brute_force_minimum(cores, universe)


@pytest.mark.parametrize("seed", range(40))
def test_result_is_a_hitting_set(seed):
    rng = random.Random(1000 + seed)
    universe = list(range(rng.randint(4, 14)))
    cores = [
        frozenset(rng.sample(universe, rng.randint(1, 4)))
        for _ in range(rng.randint(1, 10))
    ]
    answer = min_hitting_set(cores)
    assert all(answer & core for core in cores)
    assert len(answer) <= len(greedy_hitting_set(cores))


@pytest.mark.parametrize("seed", range(40))
def test_enumeration_is_complete_at_the_minimum(seed):
    """Every minimum-size hitting set must appear, since the tie break picks
    from that list and would otherwise silently miss the best repair."""
    rng = random.Random(2000 + seed)
    universe = list(range(rng.randint(4, 10)))
    cores = [
        frozenset(rng.sample(universe, rng.randint(1, 3)))
        for _ in range(rng.randint(1, 6))
    ]
    size = len(min_hitting_set(cores))
    expected = {
        frozenset(combo)
        for combo in combinations(sorted(universe), size)
        if all(set(combo) & core for core in cores)
    }
    produced = {h for h in minimal_hitting_sets(cores, size) if len(h) == size}
    assert produced == expected


def test_prune_dominated_keeps_only_minimal():
    cores = [frozenset({1, 2, 3}), frozenset({1}), frozenset({4, 5}), frozenset({4, 5, 6})]
    assert prune_dominated(cores) == [frozenset({1}), frozenset({4, 5})]


def test_no_cores_means_nothing_to_relax():
    assert min_hitting_set([]) == frozenset()
    assert minimal_hitting_sets([], 3) == [frozenset()]
