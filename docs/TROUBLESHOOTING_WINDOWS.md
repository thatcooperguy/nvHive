# Troubleshooting on Windows

nvhive runs on Windows (native, not just WSL), but there are a handful of
Windows-specific gotchas that don't show up on Linux or macOS. Every one
of these is worked around in the shipping package — this doc exists so
that if you see the underlying symptoms in a fork, a dev environment, or
an older version, you know exactly what's going on and how to fix it.

## Symptom: `UnicodeEncodeError: 'charmap' codec can't encode character '\u2717'`

**Cause:** Windows `cmd.exe` and legacy PowerShell default to the `cp1252`
code page, which can't represent Unicode box-drawing characters, check
marks, ellipses, or any of the other symbols rich/typer use in CLI
output. When a subprocess pipes output to a file or a non-UTF-8 parent,
the writer thread crashes and `stdout` ends up `None`.

**Fix:** nvhive sets `PYTHONIOENCODING=utf-8` at CLI startup so every
launch of `nvh` runs with UTF-8 I/O regardless of the console code page.
If you're invoking a Python entry point directly (not through the `nvh`
shim), set it yourself:

```bash
set PYTHONIOENCODING=utf-8
python -m nvh.cli.main status
```

For tests, `tests/test_cli_e2e.py::run_nvh` forces `encoding="utf-8",
errors="replace"` in the subprocess call — copy that pattern if you
write new subprocess-based tests.

## Symptom: `exit code 3221225477` / `0xC0000005` / "Segmentation fault"

**Cause:** Python 3.11+ on Windows uses `ProactorEventLoop` for asyncio.
When httpx/litellm leave `AsyncClient` sockets open past the end of the
event loop, the Python garbage collector eventually calls
`_ProactorBasePipeTransport.__del__`, which walks attributes on an
already-torn-down transport and access-violates. This is a known
interpreter bug: https://github.com/python/cpython/issues/81485

The symptom is that `nvh status`, `nvh ask`, `nvh webui` (any command
that issues HTTP requests via httpx) produces **the correct output**,
then **crashes on clean exit** with exit code 139 / 3221225477 /
0xC0000005. The functional work is done — it's a shutdown crash.

**Fix:** nvhive neutralizes `_ProactorBasePipeTransport.__del__` at CLI
startup (see `nvh/cli/main.py` top-of-file). By the time we're in GC
during process exit, we don't care about "did you close the transport?"
warnings — we care about not crashing. The OS reclaims sockets when the
process dies regardless.

If you're running nvhive as an embedded library (not the CLI), apply the
same patch yourself before any httpx usage:

```python
import sys
if sys.platform == "win32":
    import asyncio.proactor_events as _pe
    _pe._ProactorBasePipeTransport.__del__ = lambda self: None
```

## Symptom: `nvh webui` on port 80 fails with "Permission denied"

**Cause:** Binding to port 80 requires administrator privileges on
Windows. `nvh webui`'s smart port selection tries 80 first and falls
back to 3000–3002 / 8080, but on first launch without admin it may
still log "port 80 unavailable" before picking the fallback.

**Fix:** either run `nvh webui --port 3000` explicitly, or launch an
elevated terminal if you specifically want port 80 (needed for the
`http://nvhive/` hostname shortcut).

## Symptom: `nvh.exe` cannot be overwritten on `pip install -e .`

**Cause:** Windows holds an exclusive lock on `.exe` files that are
currently running (or have been run in a still-open shell). If you
have a terminal open that ran `nvh` recently, pip can't rewrite
`Scripts/nvh.exe` during a reinstall.

**Fix:** close every terminal window that's touched `nvh`, then retry.
If that's impractical, launch commands via `python -m nvh.cli.main`
directly — the module path doesn't use the `.exe` shim.

## Symptom: `aiosqlite` ResourceWarning or lock errors on test teardown

**Cause:** `aiosqlite` connections held open across asyncio loop
boundaries can't be cleaned up the usual way. This is cosmetic on
Windows and Linux both — it doesn't corrupt anything — but produces
noisy warnings in pytest output.

**Fix:** the warnings are benign and suppressed in CI config. If you're
writing a test that intentionally creates and tears down multiple
async DB connections, use explicit `async with repo._session() as s:`
blocks so cleanup runs inside the loop that owns the connection.

## When in doubt

Run `nvh doctor` — it produces a full diagnostic dump of platform,
Python version, asyncio policy, installed providers, and recent CLI
errors. Paste the output into a GitHub issue and it's usually enough
for a fast diagnosis.
