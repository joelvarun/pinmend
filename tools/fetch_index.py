"""Build an offline PyPI metadata snapshot for pinmend.

Fetches per-version requires_dist from the PyPI JSON API for a fixed package
list, keeps a stratified sample of each project's release history, and writes
one gzipped JSON file. Run once; the snapshot is what every benchmark and
example reads, so results are reproducible without network access.
"""

from __future__ import annotations

import gzip
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

API = "https://pypi.org/pypi"
UA = "pinmend-index-builder/1.0 (+https://github.com/joelvarun/pinmend)"

# Projects chosen to cover dependency clusters where real pin conflicts occur:
# the Flask stack, the requests/urllib3/botocore stack, the numpy/scipy stack,
# TensorFlow and PyTorch, and the pydantic/FastAPI stack.
PROJECTS = [
    "flask", "werkzeug", "jinja2", "itsdangerous", "click", "markupsafe", "blinker",
    "requests", "urllib3", "charset-normalizer", "idna", "certifi", "chardet",
    "boto3", "botocore", "s3transfer", "jmespath", "python-dateutil", "six",
    "numpy", "scipy", "pandas", "scikit-learn", "joblib", "threadpoolctl",
    "matplotlib", "pillow", "contourpy", "cycler", "fonttools", "kiwisolver",
    "pyparsing", "packaging", "pytz", "tzdata",
    "tensorflow", "protobuf", "grpcio", "absl-py", "keras", "h5py", "ml-dtypes",
    "wrapt", "gast", "opt-einsum", "termcolor", "flatbuffers", "astunparse",
    "tensorboard", "google-pasta", "libclang",
    "torch", "typing-extensions", "filelock", "sympy", "networkx", "fsspec", "mpmath",
    "pydantic", "pydantic-core", "fastapi", "starlette", "anyio", "sniffio",
    "httpx", "httpcore", "h11", "uvicorn", "annotated-types",
    "black", "pathspec", "mypy-extensions", "platformdirs", "tomli",
    "attrs", "jsonschema", "referencing", "rpds-py", "setuptools", "wheel",
]

# Older releases that real requirements files still pin and that the recent
# window would otherwise miss. Keeping them makes the conflicts real rather
# than reconstructed.
MUST_INCLUDE = {
    "flask": ["1.1.4", "2.0.1", "2.1.3", "2.2.5"],
    "werkzeug": ["1.0.1", "2.0.3", "2.2.3", "2.3.8", "3.0.1"],
    "jinja2": ["2.11.3", "3.0.3", "3.1.2"],
    "click": ["7.1.2", "8.0.4", "8.1.3"],
    "scipy": ["1.7.3", "1.9.3", "1.10.1"],
    "numpy": ["1.21.6", "1.22.4", "1.24.4", "1.26.4"],
    "pandas": ["1.1.5", "1.3.5", "1.5.3", "2.0.3"],
    "scikit-learn": ["1.0.2", "1.1.3", "1.3.2"],
    "tensorflow": ["2.11.1", "2.12.0", "2.13.1", "2.15.0"],
    "protobuf": ["3.19.6", "3.20.3", "4.21.12", "4.25.3"],
    "keras": ["2.11.0", "2.12.0", "2.13.1", "3.0.5"],
    "fastapi": ["0.95.2", "0.99.1", "0.103.2"],
    "pydantic": ["1.10.15", "2.0.3", "2.5.3"],
    "starlette": ["0.27.0", "0.36.3"],
    "botocore": ["1.29.165", "1.31.85", "1.34.51"],
    "boto3": ["1.26.165", "1.28.85", "1.34.51"],
    "urllib3": ["1.26.18", "2.0.7", "2.2.1"],
    "requests": ["2.27.1", "2.29.0", "2.31.0"],
    "torch": ["1.13.1", "2.0.1", "2.2.2"],
    "typing-extensions": ["4.5.0", "4.7.1", "4.10.0"],
    "black": ["22.3.0", "23.3.0", "24.3.0"],
    "matplotlib": ["3.5.3", "3.7.5", "3.8.4"],
    "pillow": ["9.5.0", "10.3.0"],
    "grpcio": ["1.48.2", "1.56.2", "1.62.1"],
    "h5py": ["3.8.0", "3.10.0"],
    "anyio": ["3.7.1", "4.3.0"],
}

RECENT = 14    # newest releases kept verbatim
STRATA = 16    # additional releases sampled evenly across the rest of history


def _get(url: str, tries: int = 4) -> dict | None:
    for attempt in range(tries):
        try:
            with urlopen(Request(url, headers={"User-Agent": UA}), timeout=30) as fh:
                return json.load(fh)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            time.sleep(0.5 * (attempt + 1))
        except (URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(0.5 * (attempt + 1))
    return None


def select_versions(project: str, releases: dict) -> list[str]:
    parsed = []
    for raw, files in releases.items():
        if not files or all(f.get("yanked") for f in files):
            continue
        try:
            ver = Version(raw)
        except InvalidVersion:
            continue
        if ver.is_prerelease or ver.is_devrelease:
            continue
        parsed.append((ver, raw))
    parsed.sort()
    chosen = {raw for _, raw in parsed[-RECENT:]}
    rest = parsed[:-RECENT]
    if rest and STRATA:
        step = max(1, len(rest) // STRATA)
        chosen.update(raw for _, raw in rest[::step])
    for raw in MUST_INCLUDE.get(project, []):
        if any(raw == r for _, r in parsed):
            chosen.add(raw)
    return sorted(chosen, key=Version)


def main(out_path: str) -> int:
    t0 = time.time()
    plan: list[tuple[str, str]] = []
    for project in PROJECTS:
        payload = _get(f"{API}/{project}/json")
        if payload is None:
            print(f"  skip {project}: not found", flush=True)
            continue
        versions = select_versions(project, payload.get("releases", {}))
        plan.extend((project, v) for v in versions)
        print(f"  {project}: {len(versions)} versions", flush=True)
    print(f"index plan: {len(plan)} release fetches", flush=True)

    def fetch(job: tuple[str, str]):
        project, version = job
        payload = _get(f"{API}/{project}/{version}/json")
        if payload is None:
            return None
        info = payload.get("info", {})
        return (project, version, info.get("requires_dist") or [],
                info.get("requires_python") or "")

    out: dict[str, dict] = {}
    done = 0
    with ThreadPoolExecutor(12) as pool:
        for row in pool.map(fetch, plan):
            done += 1
            if done % 200 == 0:
                print(f"  fetched {done}/{len(plan)}", flush=True)
            if row is None:
                continue
            project, version, requires, requires_python = row
            name = canonicalize_name(project)
            entry = out.setdefault(name, {"project": project, "versions": {}})
            entry["versions"][version] = {
                "requires_dist": requires,
                "requires_python": requires_python,
            }

    snapshot = {
        "schema": 1,
        "source": "pypi.org JSON API",
        "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "packages": out,
    }
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(snapshot, fh, separators=(",", ":"), sort_keys=True)
    total = sum(len(p["versions"]) for p in out.values())
    print(f"wrote {path} : {len(out)} packages, {total} releases, "
          f"{path.stat().st_size / 1e6:.2f} MB, {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "pinmend/data/pypi_snapshot.json.gz"))
