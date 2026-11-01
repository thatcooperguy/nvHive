"""Bundled data files, resolved through :mod:`importlib.resources`.

nvHive ships a few non-Python files inside the ``nvh`` package (the
``[tool.setuptools.package-data]`` globs in pyproject.toml): the model
catalog and its LiteLLM overrides under ``config/``, the benchmark prompts,
the built-in workflow templates under ``workflows/``, the setup catalog and
Agent Library under ``catalog/``, the Wizard system prompt under ``prompts/``.
The loaders that used to walk up from ``__file__``
(``Path(__file__).parent.parent / "config"``: the model catalog, the
benchmark prompts, the workflow templates) ask this module instead, so the
lookup follows the import system rather than assuming the source tree's
shape; since 0.46 the ``catalog/`` and ``prompts/`` readers (the setup
catalog, the Agent Library, the Wizard system prompt) come through here too
instead of calling ``importlib.resources`` on their own sub-packages.
tests/test_single_root.py checks that every file the globs ship resolves
here and that no module under nvh/ anchors a data path on ``__file__``;
tests/test_bundled_data_access.py checks that this is the only module under
nvh/ that touches ``importlib.resources`` at all.

Everything is anchored on the top-level ``nvh`` package and the directory
name is joined onto it (``resources.files("nvh") / "workflows"``), which
works for sub-packages and bare data directories alike -- ``nvh/workflows``
has no ``__init__.py``, so one anchor spares every caller the question.

nvHive is installed as plain files (a wheel, an editable checkout), so
:func:`bundled_path` hands back the real :class:`~pathlib.Path` the Path-based
APIs want (``ProviderRegistry.load_capabilities(path)``,
``discover_workflows() -> dict[str, Path]``). A zipped or frozen install is
not a layout nvHive supports: there :func:`bundled_path` raises instead of
pointing at a path that does not exist, while :func:`bundled` and the
``read_*`` helpers keep working.
"""

from __future__ import annotations

import json
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import yaml

__all__ = ["PACKAGE", "bundled", "bundled_path", "read_json", "read_text", "read_yaml"]

PACKAGE = "nvh"


def bundled(*parts: str) -> Traversable:
    """The bundled file or directory ``nvh/<parts...>``.

    ``bundled("config", "capabilities.yaml")`` is the catalog,
    ``bundled("workflows")`` the built-in workflow directory. The result
    answers ``is_file()``, ``is_dir()``, ``iterdir()``, ``read_text()`` and
    ``read_bytes()`` whatever the install layout; it is not checked for
    existence here, so callers keep their own ``is_file()`` / ``is_dir()``
    guards where a missing file has a fallback.
    """
    node: Traversable = resources.files(PACKAGE)
    for part in parts:
        node = node / part
    return node


def bundled_path(*parts: str) -> Path:
    """``bundled(*parts)`` as a filesystem :class:`~pathlib.Path`.

    Raises :class:`FileNotFoundError` when the package is not installed as
    plain files (see the module docstring). Like :func:`bundled` it does not
    check that the file itself exists.
    """
    node = bundled(*parts)
    if isinstance(node, Path):
        return node
    raise FileNotFoundError(
        f"nvh/{'/'.join(parts)} is not on the filesystem ({type(node).__name__}); "
        "nvHive expects a wheel or editable install"
    )


def read_text(*parts: str, encoding: str = "utf-8") -> str:
    """The bundled file's text (UTF-8 unless told otherwise)."""
    return bundled(*parts).read_text(encoding=encoding)


def read_json(*parts: str) -> Any:
    """The bundled JSON file, parsed."""
    return json.loads(read_text(*parts))


def read_yaml(*parts: str) -> Any:
    """The bundled YAML file, parsed with ``yaml.safe_load`` (``None`` for an empty file)."""
    return yaml.safe_load(read_text(*parts))
