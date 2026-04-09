"""Version consistency check.

`pyproject.toml` and `nvh/__init__.py` both declare the package
version, and they've drifted before (I've had to find-and-replace
both on every release). This test catches that skew in CI before a
mismatched wheel hits PyPI.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import nvh


def _pyproject_version() -> str:
    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def test_version_matches_pyproject():
    """`nvh.__version__` must equal `pyproject.toml::project.version`.

    If this fails, the release is half-bumped and `nvh version` will
    print the wrong number even though PyPI gets the right one.
    """
    pyproject = _pyproject_version()
    runtime = nvh.__version__
    assert runtime == pyproject, (
        f"Version skew: nvh.__version__={runtime!r} "
        f"but pyproject.toml has {pyproject!r}. "
        f"Update one of them so they match."
    )
