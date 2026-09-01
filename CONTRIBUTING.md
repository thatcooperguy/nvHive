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
5. **Sign your commits**: `git commit -s -m "your message"` (see DCO below).
6. Open a PR against `main` with a clear description of what and why.

## Developer Certificate of Origin (DCO)

Every commit in a PR must carry a `Signed-off-by:` line. We use the
[Developer Certificate of Origin 1.1](https://developercertificate.org/) —
the same lightweight contribution model the Linux kernel, Docker, and most
Linux Foundation projects use. By signing off you certify that:

- You have the right to submit the contribution under the project's license.
- The contribution is your own work, or you have permission to submit it.
- You understand the contribution and the signature are public.

There is no separate Contributor License Agreement to sign — the sign-off
is the agreement. Add it with:

```bash
git commit -s -m "your message"
# or amend the last commit:
git commit --amend --signoff
```

CI (`.github/workflows/dco.yml`) verifies every commit in a PR carries a
sign-off line. PRs with unsigned commits will be asked to add `-s` and
force-push.

## Inbound License and Project Identity

By submitting a contribution, you agree that your contribution is licensed under
the PolyForm Noncommercial License 1.0.0 used by this repository (see `LICENSE`;
versions 0.40.0 and earlier were published under MIT and remain MIT).

The official nvHive project is maintained at
https://github.com/thatcooperguy/nvHive and distributed on PyPI as `nvhive`.
Forks are welcome, but independent redistributions should use distinct project
names, package names, release channels, and branding. See `NOTICE.md` and
`TRADEMARKS.md`.

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
