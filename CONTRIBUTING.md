# Contributing to nvHive

Thank you for your interest in contributing! The developer docs are
[docs/TESTING.md](docs/TESTING.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
and, for release mechanics, [docs/MAINTAINERS.md](docs/MAINTAINERS.md).

## Setting up

```bash
git clone https://github.com/thatcooperguy/nvHive && cd nvHive
python -m venv .venv && source .venv/bin/activate     # Python 3.11 or 3.12
pip install -e ".[dev]"
python -m pytest tests/ -q
```

The WebUI is a separate Node 22 project: `cd web && npm ci && npm run dev`.

## How to Add a Provider

1. Add a `ProviderSpec` row to `nvh/providers/specs.py` (default and fallback
   model, LiteLLM prefix, base URL, extra env vars). `OpenAICompatibleProvider`
   supplies the behaviour and `tests/test_providers_parametrized.py` picks the
   row up automatically.
2. Add the default and fallback models to `nvh/config/capabilities.yaml` and the
   stanza to `generate_default_config` in `nvh/config/settings.py`; parity tests
   enforce both.
3. Add an entry to `KNOWN_ADVISORS` in `nvh/cli/main.py` and a row to
   `docs/PROVIDERS.md`.
4. A provider that is not OpenAI-compatible (its own transport or discovery, like
   Ollama or Triton) gets a bespoke adapter listed in `BESPOKE_ADAPTERS` in
   `nvh/providers/registry.py`, with tests in `tests/test_providers_special.py`.

## How to Add a Plugin

Plugins extend nvHive with a provider, an agent persona or a cabinet without
touching the package. `nvh/plugins/manager.py` discovers them two ways:

- a Python file in `~/.hive/plugins/` that defines the class and a manifest:

  ```python
  NVHIVE_PLUGIN = {"type": "provider", "name": "my_provider", "class": MyProvider}
  ```

- a pip package exposing an entry point in the `nvhive.plugins` group:

  ```toml
  [project.entry-points."nvhive.plugins"]
  my_provider = "my_package:MyProvider"
  ```

`nvh plugins` lists what was found.

## Tests and layout

`tests/` is flat: one `test_<subject>.py` per module or feature, no
subdirectories. Add tests beside the subject you changed, delete them with the
module, and never rely on network, API keys or a running Ollama in the default
run (use `MockProvider` or patch `litellm`). Details and the guard tests that
read docs are in [docs/TESTING.md](docs/TESTING.md).

```bash
python -m pytest tests/test_<subject>.py -q
python -m ruff check nvh/ tests/ --ignore E501,E402,N806,E702,F841     # CI's rule set
python -m mypy nvh/sandbox nvh/catalog --strict --follow-imports=silent --ignore-missing-imports
```

mypy gates only the modules that are already clean (`nvh/sandbox`,
`nvh/catalog`); a repo-wide run is informational. When a module you touch
reaches zero errors, add it to the gated list in `.github/workflows/ci.yml`.

## Documentation

- `docs/COMMANDS.md` is generated — run `python scripts/gen_commands_doc.py`
  after adding, renaming or hiding a command; CI diffs it.
- Do not type inventory counts (providers, models, free tiers, cabinets, tools,
  personas, agents) into README, docs or CLI help. `tests/test_marketing_parity.py`
  fails on any count that disagrees with the code.
- Relative links and images in README and `docs/` must resolve
  (`tests/test_docs_links.py`).
- One page per topic; the set is listed in the README. Extend an existing page
  rather than adding a new file.

## Submitting Pull Requests

1. Fork the repo and create a feature branch: `git checkout -b feat/my-feature`
2. Make your changes and add tests.
3. Run the tests, the linter and the gated type check above.
4. **Sign your commits**: `git commit -s -m "your message"` (see DCO below).
5. Open a PR against `main` with a clear description of what and why.

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

- Python 3.11+, type-annotated, `from __future__ import annotations`
- Formatter: `ruff format`; linter: `ruff check` with the rule set above
- Keep functions focused; prefer composition over large classes.
- No new parallel implementations: plug into the existing engine, tool
  registry, chat store and `NVH_HOME` layout (see the Non-goals in
  [docs/ROADMAP.md](docs/ROADMAP.md)).

## Reporting Bugs

Open a GitHub Issue with:
- nvHive version (`nvh version`)
- OS and Python version
- Steps to reproduce
- Expected vs actual behavior
- Output of `nvh status --deep`, or the redacted bundle from `nvh status --report`
