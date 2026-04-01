# Contributing to NVHive

Thank you for your interest in contributing!

## How to Add a Provider

1. Create `nvh/providers/<name>.py` implementing the `BaseProvider` interface.
2. Register it in `nvh/providers/__init__.py` and `nvh/config/settings.py`.
3. Add an entry to `KNOWN_ADVISORS` in `nvh/cli/main.py`.
4. Add tests under `tests/providers/test_<name>.py`.

## How to Create a Plugin

Plugins live in `~/.hive/plugins/` or the `nvh/plugins/` directory.

1. Create a directory: `my_plugin/`
2. Add `my_plugin/plugin.yaml` with `name`, `version`, `hooks`, and `tools` fields.
3. Implement hook handlers as Python callables referenced in `plugin.yaml`.
4. Install with `nvh plugins install ./my_plugin`.

See `docs/plugins.md` for the full plugin API reference.

## Submitting Pull Requests

1. Fork the repo and create a feature branch: `git checkout -b feat/my-feature`
2. Make your changes and add tests.
3. Run the test suite: `pytest tests/`
4. Run the linter: `ruff check nvh/` and `mypy nvh/`
5. Open a PR against `main` with a clear description of what and why.

## Code Style

- Python 3.12+, type-annotated, `from __future__ import annotations`
- Formatter: `ruff format`
- Linter: `ruff check` + `mypy --strict`
- Keep functions focused; prefer composition over large classes.

## Reporting Bugs

Open a GitHub Issue with:
- NVHive version (`nvh version`)
- OS and Python version
- Steps to reproduce
- Expected vs actual behavior
- Output of `nvh doctor`
