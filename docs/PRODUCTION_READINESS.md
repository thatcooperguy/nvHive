# Production Readiness

nvHive can be CI-clean without being production-ready for the target cloud
desktop. The production bar is the real rootless NVIDIA Linux VM with persistent
block storage, because that is where drivers, CUDA, Python, storage, display,
and model downloads all meet.

## Readiness States

The setup API exposes a conservative report at:

```bash
GET /v1/setup/production-readiness
```

The report returns:

- `blocked`: one or more gates must be fixed before beta or production.
- `pilot-ready`: no hard blockers, but target VM validation or warnings remain.
- `production-ready`: all gates pass and the target VM acceptance flag is set.

The report is intentionally conservative. It will not mark production-ready
until a real NVIDIA Linux VM test has been completed and
`NVH_TARGET_VM_VALIDATED=1` is present for the final check.

## Gates

The readiness report checks:

- Persistent `NVH_HOME` is writable and explicitly configured.
- Mount autopilot can find or validate the persistent block-backed home.
- Python runtime can use normal venv/pip or the rootless micromamba fallback.
- The target Linux NVIDIA GPU session exposes driver, CUDA, and VRAM facts.
- App compatibility has no blocked items.
- Boot preflight has a stable baseline and no unexpected VM image drift.
- Smoke tests have no failures.
- Recommended local model queue fits persistent storage.
- Install receipts are healthy.
- All one-click Studio packs are marked no-root.
- The real target VM acceptance run has been completed.

## Target VM Acceptance Checklist

Run this on a fresh NVIDIA Linux cloud desktop without root access:

1. Install from GitHub or PyPI into the user-owned persistent mount.
2. Confirm `NVH_HOME` lands on the 200 GB+ block-backed mount, not the OS disk
   or a read-only share.
3. Launch the WebUI from the desktop launcher.
4. Install **AI Starter** and verify Ollama plus the recommended model queue.
5. Install **Graphics Creator Studio** and launch ComfyUI with starter examples.
6. Install **Game Dev Lab** and verify Blender/Godot helper launchers.
7. Install **Music Producer Studio** and verify helper workspaces without sudo.
8. Reboot or reconnect the VM and confirm boot preflight reports a stable image.
9. Run the readiness report again with:

```bash
export NVH_TARGET_VM_VALIDATED=1
nvh webui
```

Then open Advanced Details in the setup wizard and verify Release Readiness is
`production-ready`.

## Logging and Error Reports

The API attaches an `X-Request-ID` header to every response and includes that id
in structured logs. When `NVH_HOME` or `NVH_LOGS` is active, nvHive also writes
rootless logs under the persistent mount, usually:

```bash
$NVH_HOME/logs/nvhive.log
```

The setup wizard has **Advanced Details -> Copy Error Report**. It calls:

```bash
GET /v1/setup/diagnostics
```

That report includes storage status, release gates, recent setup jobs, install
receipts, safe environment facts, and recent warning/error log lines. API keys,
bearer tokens, GitHub tokens, and common secret-shaped values are redacted before
the report is shown or copied.

## Release Rule

Use this language in releases:

- Before target VM validation: "beta" or "pilot-ready".
- After the checklist passes on the NVIDIA Linux VM: "production-ready".

Do not publish a PyPI release as production-ready if the report is still
`pilot-ready` or `blocked`.
