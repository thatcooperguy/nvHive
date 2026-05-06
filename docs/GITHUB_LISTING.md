# GitHub Listing Checklist

Use this when polishing the public repository page before a release.

## Repository Summary

Short description:

> Rootless NVIDIA AI lab for Linux GPU desktops: local LLMs, ComfyUI, agents, creative tools, game-dev tools, music tools, and a self-healing setup wizard.

Website:

> https://pypi.org/project/nvhive/

Suggested topics:

```text
nvidia
gpu
llm
local-ai
comfyui
ollama
agents
rootless
linux-desktop
ai-workstation
student-tools
generative-ai
```

## Pinned Release Message

Until the target NVIDIA Linux VM acceptance run passes, describe the release as:

> nvHive is pilot-ready. CI is green and the rootless setup path is implemented, but production-ready status waits for the real no-root NVIDIA Linux VM validation.

After the target VM checklist passes:

> nvHive is production-ready for the validated no-root NVIDIA Linux GPU desktop profile.

## Visual Assets

Current README assets:

- `docs/screenshots/terminal-demo-v2.gif`
- `docs/screenshots/webui-walkthrough.gif`
- `docs/screenshots/rootless-runtime.svg`
- `docs/screenshots/setup-flow.svg`
- `docs/screenshots/smart-router.svg`
- `docs/screenshots/architecture.svg`

Refresh screenshots/GIFs after major UI changes:

```bash
cd web
npm run build
cd ..
node docs/capture_gif.mjs
```

The capture script expects the WebUI to be reachable through `WEBUI_URL`
or `http://localhost:3000`.

## Before Publishing PyPI

- Fresh no-root install on the target NVIDIA Linux VM.
- `NVH_HOME` auto-detects the persistent 200GB+ block-backed mount.
- WebUI opens from the desktop launcher.
- `/v1/ready` is not blocked.
- AI Starter install succeeds.
- ComfyUI install/start succeeds and writes examples.
- Model recommendations match detected GPU/VRAM.
- Reconnect/reboot boot preflight catches no unexpected drift.
- Support snapshot redacts secrets and local paths.
