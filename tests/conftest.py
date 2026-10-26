"""Shared fixtures: a tiny in-memory index builder and the real snapshot."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pinmend.index import Dep, Package, PackageIndex, Release

SNAPSHOT = Path(__file__).resolve().parent.parent / "pinmend" / "data" / "pypi_snapshot.json.gz"


def make_index(spec: dict[str, dict[str, list[str]]]) -> PackageIndex:
    """Build an index from {package: {version: [requirement strings]}}."""
    packages: dict[str, Package] = {}
    for name, versions in spec.items():
        key = canonicalize_name(name)
        pkg = Package(name=key, project=name)
        for raw, requires in versions.items():
            deps = []
            for text in requires:
                req = Requirement(text)
                deps.append(
                    Dep(
                        name=canonicalize_name(req.name),
                        specifier=req.specifier,
                        extras=frozenset(),
                        spec_text=str(req.specifier),
                    )
                )
            pkg.releases.append(Release(Version(raw), raw, tuple(deps), {}))
        pkg.finalize()
        packages[key] = pkg
    return PackageIndex(packages, {"source": "synthetic"})


def random_index(rng: random.Random, packages: int = 6, versions: int = 4) -> PackageIndex:
    """A random dependency universe, used to fuzz the solver against itself."""
    names = [chr(ord("a") + i) for i in range(packages)]
    spec: dict[str, dict[str, list[str]]] = {}
    for i, name in enumerate(names):
        spec[name] = {}
        for v in range(1, versions + 1):
            requires = []
            for other in names[i + 1:]:
                if rng.random() < 0.35:
                    low = rng.randint(1, versions)
                    high = rng.randint(low, versions + 1)
                    requires.append(f"{other}>={low}.0,<{high}.0")
            spec[name][f"{v}.0"] = requires
    return make_index(spec)


def random_roots(rng: random.Random, index: PackageIndex, count: int) -> list[str]:
    names = rng.sample(sorted(index.packages), min(count, len(index.packages)))
    lines = []
    for name in names:
        release = rng.choice(index.packages[name].releases)
        lines.append(f"{name}=={release.raw}")
    return lines


@pytest.fixture(scope="session")
def real_index() -> PackageIndex:
    if not SNAPSHOT.exists():
        pytest.skip("run tools/fetch_index.py to build the snapshot")
    return PackageIndex.load(SNAPSHOT)
