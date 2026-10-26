"""Offline package index: snapshot loading, marker evaluation, version ordering."""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from packaging.markers import Marker, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

# One fixed target environment keeps marker evaluation, and therefore every
# number in the benchmark, identical on any machine that reads the snapshot.
TARGET_ENV = {
    "implementation_name": "cpython",
    "implementation_version": "3.10.13",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "5.15.0",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.10.13",
    "python_version": "3.10",
    "sys_platform": "linux",
}

_EXTRA_RE = re.compile(r"""extra\s*==\s*['"]([^'"]+)['"]""")


@dataclass(frozen=True)
class Dep:
    """One resolved dependency edge: a package, a range, and requested extras.

    spec_text is the canonical specifier string, kept alongside the parsed
    object because it is the cache key for candidate lookups and is otherwise
    recomputed tens of thousands of times per repair.
    """

    name: str
    specifier: SpecifierSet
    extras: frozenset[str]
    spec_text: str = ""

    def __str__(self) -> str:
        extras = f"[{','.join(sorted(self.extras))}]" if self.extras else ""
        return f"{self.name}{extras}{self.spec_text}"


@dataclass
class Release:
    version: Version
    raw: str
    deps_base: tuple[Dep, ...]
    deps_extra: dict[str, tuple[Dep, ...]]
    idx: int = -1


@dataclass
class Package:
    name: str
    project: str
    releases: list[Release] = field(default_factory=list)
    _rank: dict[Version, int] = field(default_factory=dict)

    def rank(self, version: Version) -> int:
        """Position of a version in ascending release order, -1 if absent."""
        return self._rank.get(version, -1)

    def finalize(self) -> None:
        self.releases.sort(key=lambda r: r.version)
        for i, release in enumerate(self.releases):
            release.idx = i
        self._rank = {r.version: i for i, r in enumerate(self.releases)}


def _split_marker(marker: Marker | None) -> tuple[bool, set[str]]:
    """Return (holds with no extra, extras under which it holds)."""
    if marker is None:
        return True, set()
    try:
        base = marker.evaluate({**TARGET_ENV, "extra": ""})
    except UndefinedEnvironmentName:
        base = False
    holds_for: set[str] = set()
    for extra in set(_EXTRA_RE.findall(str(marker))):
        try:
            if marker.evaluate({**TARGET_ENV, "extra": canonicalize_name(extra)}):
                holds_for.add(canonicalize_name(extra))
        except UndefinedEnvironmentName:
            continue
    return base, holds_for


def _parse_requires(requires_dist: list[str]) -> tuple[tuple[Dep, ...], dict[str, tuple[Dep, ...]]]:
    base: list[Dep] = []
    per_extra: dict[str, list[Dep]] = {}
    for raw in requires_dist:
        # Metadata 1.x wrapped specifiers in parentheses: "Werkzeug (>=2.0)".
        cleaned = raw.replace("(", "").replace(")", "")
        try:
            req = Requirement(cleaned)
        except InvalidRequirement:
            continue
        dep = Dep(
            name=canonicalize_name(req.name),
            specifier=req.specifier,
            extras=frozenset(canonicalize_name(e) for e in req.extras),
            spec_text=str(req.specifier),
        )
        holds_base, holds_for = _split_marker(req.marker)
        if holds_base:
            base.append(dep)
        for extra in holds_for:
            per_extra.setdefault(extra, []).append(dep)
    return tuple(base), {k: tuple(v) for k, v in per_extra.items()}


class PackageIndex:
    """Immutable view over a snapshot, restricted to the target environment."""

    def __init__(self, packages: dict[str, Package], meta: dict):
        self.packages = packages
        self.meta = meta
        # Candidate sets are looked up thousands of times per repair, always
        # for the same handful of specifiers, so each (package, specifier) pair
        # is resolved once into a bitmask over that package release list.
        self._mask_cache: dict[tuple[str, str], int] = {}
        known = set(packages)
        self.unknown_refs = {
            dep.name
            for pkg in packages.values()
            for rel in pkg.releases
            for dep in rel.deps_base
            if dep.name not in known
        }

    @classmethod
    def load(cls, path: str | Path) -> "PackageIndex":
        p = Path(path)
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", encoding="utf-8") as fh:
            snapshot = json.load(fh)
        packages: dict[str, Package] = {}
        for name, entry in snapshot["packages"].items():
            pkg = Package(name=name, project=entry["project"])
            for raw, info in entry["versions"].items():
                try:
                    version = Version(raw)
                except InvalidVersion:
                    continue
                if not _python_ok(info.get("requires_python", "")):
                    continue
                deps_base, deps_extra = _parse_requires(info.get("requires_dist") or [])
                pkg.releases.append(Release(version, raw, deps_base, deps_extra))
            if pkg.releases:
                pkg.finalize()
                packages[name] = pkg
        meta = {k: v for k, v in snapshot.items() if k != "packages"}
        return cls(packages, meta)

    def get(self, name: str) -> Package | None:
        return self.packages.get(canonicalize_name(name))

    def mask(self, name: str, spec_text: str) -> int:
        """Bitmask over the releases of a package that satisfy a specifier."""
        key = (name, spec_text)
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached
        pkg = self.packages.get(name)
        if pkg is None:
            self._mask_cache[key] = 0
            return 0
        spec = SpecifierSet(spec_text)
        bits = 0
        for i, rel in enumerate(pkg.releases):
            if spec.contains(rel.version, prereleases=True):
                bits |= 1 << i
        self._mask_cache[key] = bits
        return bits

    def full_mask(self, name: str) -> int:
        pkg = self.packages.get(name)
        return (1 << len(pkg.releases)) - 1 if pkg else 0

    def decode(self, name: str, bits: int) -> list[Release]:
        releases = self.packages[name].releases
        out: list[Release] = []
        i = 0
        while bits:
            if bits & 1:
                out.append(releases[i])
            bits >>= 1
            i += 1
        return out

    def release_count(self) -> int:
        return sum(len(p.releases) for p in self.packages.values())

    def deps_of(self, name: str, release: Release, extras: frozenset[str]) -> list[Dep]:
        deps = list(release.deps_base)
        for extra in extras:
            deps.extend(release.deps_extra.get(extra, ()))
        return [d for d in deps if d.name in self.packages]


def _python_ok(requires_python: str) -> bool:
    if not requires_python:
        return True
    try:
        return SpecifierSet(requires_python).contains(
            TARGET_ENV["python_full_version"], prereleases=True
        )
    except InvalidSpecifier:
        return True
