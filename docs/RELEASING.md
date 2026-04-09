# Releasing nvhive

The nvhive release flow is mostly automated but relies on one human in
the loop: a maintainer who can push to GitHub with a `workflow`-scoped
token and who has access to the PyPI trusted-publishing setup. This
document is the runbook for that maintainer.

## TL;DR

```bash
# 1. Ensure main is clean and green
git checkout main && git pull --ff-only && git status
python -m pytest tests/ -q

# 2. Bump version (keep pyproject.toml and nvh/__init__.py in sync)
#    The tests/test_version.py guard will fail CI if you forget one.
python -m nvh._dev.bump_version 0.7.0  # or edit by hand

# 3. Update CHANGELOG.md with the new section

# 4. Commit, tag, push
git commit -am "chore(release): bump version to 0.7.0"
git tag v0.7.0
git push origin main --tags

# 5. Create a GitHub Release from the tag. Publishing the release
#    triggers .github/workflows/publish.yml which builds and uploads
#    the wheel via OIDC trusted publishing — no tokens to paste.
```

## Prerequisites

You need:

- **GitHub auth** with push access to `thatcooperguy/nvhive.git` **and**
  the `workflow` scope if you plan to modify anything under
  `.github/workflows/`. Fine-grained tokens and classic OAuth tokens
  both work; GitHub's web UI and `gh` CLI are fine.
- **PyPI trusted publishing** already configured for this project.
  Check at https://pypi.org/manage/project/nvhive/settings/publishing/
  — the workflow file is `publish.yml`, environment name `pypi`,
  repository `thatcooperguy/nvhive`. If trusted publishing breaks, the
  fallback is an API token in a secret called `PYPI_API_TOKEN`.
- **A clean working tree** on `main`. `git status` should show nothing.

## Pre-release checks

Before tagging, verify:

1. **Tests green locally** (`python -m pytest tests/ -q`). CI runs the
   same suite across Linux, Windows, and macOS on every push — a
   failing matrix is a release blocker.
2. **Webui type-checks and builds**. `cd web && npx tsc --noEmit && npm
   run build`. If either fails, don't ship — the webui will break on
   first run for users.
3. **Version consistency**. `tests/test_version.py` catches skew between
   `pyproject.toml::project.version` and `nvh.__version__`, but it only
   runs if you run tests. Do.
4. **CHANGELOG updated**. Every release must have a corresponding
   section in `CHANGELOG.md`. This is how users know what they're
   getting into.
5. **CORS origins still cover `nvh webui` defaults**. If you've touched
   `nvh/api/server.py::ALLOWED_ORIGINS`, verify that `http://nvhive`,
   `http://localhost:3000`, and port 80 are all present.

## Creating the release

```bash
# Tag format MUST be vX.Y.Z (lowercase 'v'). The publish workflow
# triggers on tag creation and parses the version out of the tag name.
git tag v0.7.0
git push origin v0.7.0
```

Then go to https://github.com/thatcooperguy/nvHive/releases/new and
create a release from the tag. Paste the relevant `CHANGELOG.md`
section as the release notes. Clicking "Publish release" triggers
`.github/workflows/publish.yml` which:

1. Builds `dist/*.whl` and `dist/*.tar.gz`
2. Runs `twine check dist/*`
3. Uploads to PyPI via OIDC (no tokens)

Watch the workflow run in the Actions tab. Typical duration is 2–3
minutes. On success, `pip install nvhive==0.7.0` becomes live on PyPI
within seconds.

## Verifying the release

```bash
# In a throwaway venv
python -m venv /tmp/verify && /tmp/verify/bin/pip install nvhive==0.7.0
/tmp/verify/bin/nvh version  # must print 0.7.0
/tmp/verify/bin/nvh --help
```

If the version number is wrong or imports fail, **revoke the release
immediately** via `yanking` on PyPI (not deletion — yanking hides it
from fresh installs but preserves it for pinned users who already
have it):

```
https://pypi.org/manage/project/nvhive/release/0.7.0/
→ Options → Yank release
```

Then fix forward with a patch release (0.7.1), not a deletion.

## Rollback

If users report that 0.7.0 is broken, advise them to pin the previous
version:

```bash
pip install 'nvhive==0.6.0'
```

Or, if `nvh webui` is the problem, run `nvh webui --clean` to force a
rebuild of the bundled Next.js output from the current package
version. This fixes webui-vs-backend drift in 90% of cases.

If the breakage is in a hot code path and you need to get a fix out
fast, the workflow is:

1. Revert the bad commit on `main` (`git revert <sha>`)
2. Bump to a patch version (`0.7.1`)
3. Go through the release flow again

Never force-push or delete released tags. Both PyPI and users' local
caches keep the old version alive anyway, and it breaks reproducible
builds for anyone who pinned the bad version.

## Manual CI workflow changes

If you need to edit `.github/workflows/ci.yml` or `publish.yml`, the
push must come from a token with `workflow` scope. Most of this
project's automation uses default repo-scoped tokens, which will be
rejected with `refusing to allow ... without 'workflow' scope`. When
that happens:

1. Stage the change locally: `git add .github/workflows/ci.yml`
2. Commit it
3. Push from a shell that has the workflow-scoped token configured
   (the GitHub CLI with `gh auth refresh -s workflow` is usually the
   fastest way to get there).

The repo has `.github/ci.yml.pending-workflow-scope` as a convenient
staging area for workflow changes made by automation that lacks the
scope — anything in that file is intended to be applied manually.
