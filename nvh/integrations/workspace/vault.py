"""Rootless nvHive Vault and optional Obsidian support.

The vault is plain Markdown under ``NVH_HOME``. Obsidian is treated as an
optional viewer/editor, not as the storage layer, so memory survives even when
Obsidian is not installed.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import stat
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh.integrations.workspace.storage import ensure_storage, storage_layout

OBSIDIAN_RELEASES_API = "https://api.github.com/repos/obsidianmd/obsidian-releases/releases/latest"
OBSIDIAN_DOWNLOAD_PAGE = "https://obsidian.md/download"

VAULT_DIRS = [
    "System",
    "Models",
    "ComfyUI",
    "Projects",
    "Troubleshooting",
    "Wizard Memory",
    "Decisions",
    "Conflicts",
    "Process Playbooks",
]

KNOWN_CONFLICTS = [
    {
        "title": "Vault vs Obsidian",
        "summary": (
            "nvHive Vault is the durable Markdown memory. Obsidian is only an "
            "optional viewer so setup never depends on a GUI app."
        ),
    },
    {
        "title": "Latest vs compatible",
        "summary": (
            "Installers should prefer latest compatible stable releases, but allow "
            "pins when a class, image, or support team needs repeatability."
        ),
    },
    {
        "title": "Automatic model download vs consent",
        "summary": (
            "The wizard can recommend and count down before downloading large "
            "models, but users need a cancel path for bandwidth and disk control."
        ),
    },
    {
        "title": "Default browser profile vs isolated profile",
        "summary": (
            "Rootless launch should avoid corrupting VM browser profiles, while "
            "still allowing an override for users already signed into Firefox."
        ),
    },
    {
        "title": "OS disk vs persistent mount",
        "summary": (
            "Everything mutable belongs under NVH_HOME on the block-backed mount; "
            "the OS image can be read-only or replaced between sessions."
        ),
    },
    {
        "title": "Cloud API convenience vs secret safety",
        "summary": (
            "Provider key helpers should link to official key pages and store keys "
            "in rootless config, never in the Markdown vault by default."
        ),
    },
    {
        "title": "Heavy creative tools vs one-click simplicity",
        "summary": (
            "Game engines, Blender assets, and music tools need disk/time estimates "
            "and graceful deferral when a student only wants local AI chat."
        ),
    },
    {
        "title": "AppImage convenience vs FUSE availability",
        "summary": (
            "Obsidian and other AppImages may need FUSE on some Linux images, so "
            "nvHive should report the fallback instead of asking for sudo."
        ),
    },
]


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._ -]+", "", value).strip().replace(" ", "-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:80] or "note"


def _vault_root(home_dir: str | Path | None = None) -> Path:
    return storage_layout(home_dir).home / "vault"


def _write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return True


def _markdown_count(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for path in root.rglob("*.md") if path.is_file())


def _seed_notes(vault: Path, home: Path) -> dict[str, str]:
    """Seed the vault with starter notes wired for Obsidian Graph view.

    Every note uses Obsidian-style ``[[wikilinks]]`` so the Graph view shows
    real structure rather than disconnected dots. Every note also carries
    a YAML frontmatter ``tags`` block so the Graph view's color-by-tag
    feature produces visible clusters (wizard / decision / conflict /
    playbook / setup). The ``MAP.md`` hub note is the navigational
    starting point and the densest node in the graph.
    """
    seeded: dict[str, str] = {}

    files = {
        # ── README — the entry point. Points at the MOC, plus the hard
        #    rules every other note depends on.
        "README.md": f"""---
tags: [vault, setup]
---
# nvHive Vault

This is the durable Markdown memory for this nvHive workspace. The
[[MAP]] note is the navigational hub — start there.

- Rootless location: `{vault}`
- Workspace home: `{home}`
- Format: plain Markdown with Obsidian-style `[[wikilinks]]`
- Obsidian: optional viewer/editor — Graph view shows the full mesh

## Quick links

- [[MAP]] — Map of Content
- [[AI Wizard]] — what the in-product agent remembers
- [[Product Direction]] — current product bets
- [[Product Conflicts]] — known tradeoffs to explain, not hide
- [[Pilot Test Checklist]] — pre-release pass on a fresh GPU VM
- [[Support Report Flow]] — handling user failures

## Rules

- Do NOT paste API keys, tokens, or secrets here. The vault is durable
  Markdown — anything written is also durable. Provider keys belong in
  rootless config, not in notes.
- Wikilinks: prefer `[[Note Title]]` over `[note](path/note.md)` so
  Obsidian's Graph view and backlinks pane keep working.
- Tags: each note should carry one or more tags in the YAML
  frontmatter so the Graph clusters by topic.
""",
        # ── The hub. This is the densest node in the Graph view — every
        #    other note links back to it via the README, and it links
        #    forward to every category.
        "MAP.md": """---
tags: [vault, moc]
---
# Map of Content

This note is the hub of the vault. Every category links from here so
Obsidian's Graph view shows real structure.

## Wizard

- [[AI Wizard]] — persona, rules, and what the agent remembers

## Product

- [[Product Direction]] — what we're shipping and why
- [[Product Conflicts]] — tradeoffs the Wizard should explain
- [[Multi-Expert Council]] — when to convene 23 personas instead of one Wizard
- [[GPU Capability Matrix]] — VRAM tiers and what auto-enables at each one

## Playbooks

- [[Pilot Test Checklist]] — gate before calling a build ready
- [[Support Report Flow]] — handling user failure reports
- [[Headless QA Loop]] — automated install verification on a rented cloud rig
- [[Service Order]] — boot sequence, health-wait contracts, env knobs

## Troubleshooting

- [[Common Install Issues]] — port conflicts, stale API,
  Ollama not starting, browser not opening, dark mode panels

## Conventions

- Add a new note with a YAML `tags:` block listing one or more of
  `wizard`, `decision`, `conflict`, `playbook`, `setup`.
- Cross-link aggressively — `[[Other Note]]` is cheap and the Graph
  view rewards density.
- Re-link this MAP from any new top-level category note so the hub
  stays connected.
""",
        "Wizard Memory/AI Wizard.md": """---
tags: [wizard, decision]
---
# AI Wizard

The in-product agent for this rootless NVIDIA AI workspace. See
[[Product Direction]] for the broader product bets and
[[Product Conflicts]] for the tradeoffs the Wizard should explain.

## Core rules

- Never assume root or sudo access.
- Prefer `NVH_HOME` on the persistent block-backed mount for models,
  apps, logs, jobs, and config.
- Run boot checks because the base VM image, kernel, driver, CUDA,
  Python, Node, or browser can change between sessions.
- Keep the UI simple: show readiness, recommended actions, and mission
  cards first; hide long diagnostics behind Advanced Details.
- Manual commands are fallback overrides, not the main user journey.
- Use local AI first when ready; cloud providers only when configured.
- When something fails, summarize logs in plain English and offer safe
  rootless repairs — see [[Support Report Flow]].

## Built-in agent profiles

The Wizard ships six built-in personas the user can swap to. Each maps
to a preferred provider/model with an optional cost ceiling:

- `wizard` — calm mission-control mentor (default).
- `coder` — careful code reviewer (prefers `qwen2.5-coder:7b`).
- `researcher` — web-search-first; $0.05/turn ceiling.
- `writer` — long-form, plainspoken tone matching.
- `ops` — repair-workspace operator; whitelisted to safe tools.
- `vault-rag` — answers strictly from this vault.

## Related

- [[Pilot Test Checklist]] — what to verify before calling a build ready
- [[Support Report Flow]] — handling user-reported failures
- [[Product Direction]] — the broader product bets
- [[Multi-Expert Council]] — the escape hatch when one persona isn't enough
- [[GPU Capability Matrix]] — which capabilities the Wizard recommends
- [[Service Order]] — the boot sequence the Wizard depends on
""",
        "Decisions/Product Direction.md": """---
tags: [decision, product]
---
# Product Direction

Ideas to preserve. The [[AI Wizard]] follows these; [[Product Conflicts]]
captures the tradeoffs we've already made.

- Linux GPU desktop launch is the priority.
- Rootless install and uninstall must work without OS changes.
- The Wizard should open automatically and guide the user.
- Mission cards stay simple: AI Starter, Graphics Creator Studio,
  Game Dev Lab, Music Producer Studio, and Agent Builder.
- The Wizard should recommend models by GPU, VRAM, disk, and current
  runtime health.
- ComfyUI, Blender, Godot, local models, cloud API keys, and debugging
  should be one-click where possible.
- A local agent helper should understand this product, read local logs,
  and generate support reports — see [[Support Report Flow]].
- Boot health should detect base image drift before the user wastes time.

## Related

- [[AI Wizard]] — the in-product agent
- [[Product Conflicts]] — known tradeoffs
- [[Pilot Test Checklist]] — pre-release gate
- [[Multi-Expert Council]] — when a single Wizard answer isn't enough
- [[GPU Capability Matrix]] — what we promise on which rigs
""",
        "Conflicts/Product Conflicts.md": _conflicts_markdown(),
        "Process Playbooks/Pilot Test Checklist.md": """---
tags: [playbook, setup]
---
# Pilot Test Checklist

Run this before calling a Linux GPU VM build ready. Pairs with
[[Support Report Flow]] when something breaks during the run.

1. Fresh uninstall and reinstall from the GitHub curl command.
2. Confirm `NVH_HOME` lands on the persistent block-backed mount.
3. Confirm the browser opens to the setup wizard without manual commands.
4. Confirm boot health shows a clear ready/warn state — see
   [[AI Wizard]] for the readiness contract.
5. Download the recommended local model and run a one-question test.
6. Install ComfyUI and confirm the app launches.
7. Install one creative/game/music pack and confirm receipt/job history.
8. Copy a support report and verify secrets are redacted — flow lives
   in [[Support Report Flow]].
9. Disconnect/reconnect or reboot, then confirm the vault, models, and
   receipts persist.

## Related

- [[AI Wizard]] — the readiness states this checklist depends on
- [[Support Report Flow]] — what to do if a step fails
- [[Product Conflicts]] — tradeoffs that explain why a "warning" is
  sometimes the right state
- [[Service Order]] — the boot sequence each step gates on
- [[GPU Capability Matrix]] — what should auto-enable at this VRAM tier
- [[Headless QA Loop]] — automated variant of this checklist
""",
        "Process Playbooks/Support Report Flow.md": """---
tags: [playbook, support]
---
# Support Report Flow

When a user reports a failure. Used during the [[Pilot Test Checklist]]
and as the runbook for incoming support reports.

1. Ask them to click **Copy Support Report** from setup.
2. Check job history first, then boot health, then runtime status — the
   [[AI Wizard]] surfaces all three.
3. Confirm the failure is rootless-fixable before suggesting any
   OS-level change. The [[Product Conflicts]] note explains what we
   intentionally won't fix.
4. Prefer latest compatible downloads, but record any pin needed for
   repeatability.
5. Add the final fix or workaround back into this vault so the next
   support cycle learns from it.

## Related

- [[AI Wizard]] — the agent the user is interacting with
- [[Pilot Test Checklist]] — where many incoming reports originate
- [[Product Conflicts]] — known intentional tradeoffs
- [[Common Install Issues]] — symptom-driven entry to the same content
- [[Service Order]] — boot dependency map for log triage
""",
        "Troubleshooting/Common Install Issues.md": """---
tags: [troubleshooting, support, setup]
---
# Common Install Issues

If `curl … install.sh | bash` finished but something doesn't work, this
note is the first stop. Most "broken install" reports turn out to be
one of the six issues below. Each entry says what you'll see, what's
going on under the hood, and the smallest command that resolves it.

The universal first move is the services CLI — it encodes the boot
order and health gates from [[Service Order]]:

```bash
nvh services status      # which of the three services is unhealthy?
nvh services restart     # recycle API + WebUI (Ollama keeps its cache)
nvh services smoke-test  # "can the Wizard actually answer?"
nvh services stop        # take the stack down cleanly (--ollama for all)
```

Pairs with [[Support Report Flow]] for capturing a fresh report and
[[Pilot Test Checklist]] for the upstream gate.

## 1. WebUI shows red "API offline" banner

**Symptom:** WebUI loads but every panel is empty and a red ribbon at
the top says "The nvHive backend at http://localhost:8000 isn't
responding."

**What's happening:** the FastAPI server failed to initialize (most
often because an earlier run wrote a malformed `config.yaml` that the
engine couldn't parse). The TCP port stays held by the stale process
so the WebUI assumes "API up" — but every health probe returns 500.

**Fix:**

```bash
# The canonical recovery — kills the stale API, restarts it with a
# health gate, and re-runs the end-to-end Wizard check:
nvh services restart
# Only if nvh itself is broken, do it by hand:
fuser -k 8000/tcp 2>/dev/null || lsof -nP -iTCP:8000 -sTCP:LISTEN -t | xargs kill -TERM
nvh serve --port 8000
```

The `api_healthy` probe in `nvh/cli/services.py` does this stale-check
dance on every `nvh webui` launch too.

## 2. WebUI shows amber "Starting up…" banner

**Symptom:** an amber strip near the top says the backend is still
booting. Not red, not alarming.

**What's happening:** the API server is still importing FastAPI +
warming the engine. Cold installs take 5-15s; that window is
normal, not broken.

**Fix:** wait 20 seconds. The banner clears itself once `/v1/health`
returns 200. Override with `localStorage.setItem('nvh-banner-no-grace', '1')`
if you want the legacy "red instantly on any failure" behavior.

## 3. Wizard says "Ollama not running on 11434 — falling back to cloud"

**Symptom:** the install reports it started Ollama but the Wizard
routes every query to a cloud provider (or has no local model).

**What's happening (older installs):** `ollama serve` was launched in
the background with `&>/dev/null` and a fixed `sleep 2/3`. Cold rigs
needed longer to bind; the install moved on before the daemon was
ready. The orphan also died with the script in some shells.

**Fix:**

```bash
# The pipeline re-spawns Ollama with a real health-wait, then boots
# the API + WebUI in order:
nvh services start
```

Manual fallback if `nvh` itself is broken:

```bash
source $NVH_HOME/nvh-env.sh
nohup $NVH_HOME/bin/ollama serve > $NVH_HOME/logs/ollama.log 2>&1 &
until curl -sf http://localhost:11434/api/tags >/dev/null; do sleep 1; done
nvh webui
```

Set `NVH_OLLAMA_BOOT_TIMEOUT=30` before reinstall on slow rigs.

## 4. Install fails with "FOREIGN process on port 3000 / 8000 / 11434"

**Symptom:** install exits early with a table showing one of the
stack ports held by a non-nvHive process.

**What's happening:** the install runs `detect_port_conflicts` before
starting services. If a foreign process holds the port, the install
refuses to silently work around it (would have spawned the WebUI on
port 8080 cascade and confused everyone).

**Fix:**

```bash
# See exactly what's there:
lsof -nP -iTCP:8000 -sTCP:LISTEN   # or :3000, :11434

# Either stop that process or run the installer with the override:
NVH_PORT_CONFLICT_KILL_FOREIGN=1 bash install.sh
```

## 5. Browser doesn't open after install

**Symptom:** install completes, prints `Browser: http://localhost:3000/setup`,
then nothing visible happens.

**What's happening (older installs):** the auto-open chain tried to
download a rootless Firefox before checking whether any browser was
already installed. On rigs where the download is slow/blocked, the
WebUI server came up but no browser window showed.

**Fix:** newer installs probe pre-installed browsers first —
`NVH_BROWSER` override, then an existing rootless Firefox, then system
Firefox, then Chromium / Chrome / Brave / Edge — and download a
rootless Firefox only as a last resort. On older installs:

```bash
# Manually point your installed browser at the running WebUI:
xdg-open http://localhost:3000/setup   # Linux
open http://localhost:3000/setup       # macOS
```

## 6. WebUI panels look light/white even in dark mode

**Symptom:** dark theme is active but a card here and there has a
white background.

**What's happening:** older versions hardcoded `bg-[#ffffff]` /
`bg-[#fafafa]` on a handful of components without the `dark:`
variant. Newer builds add the variants everywhere.

**Fix:** `nvh update` (or pull the latest release). All built-in
pages now flip cleanly. If your custom WebUI extension has a stale
hex color, add a `dark:` Tailwind sibling next to the light one.

## Related

- [[Pilot Test Checklist]] — pre-release gate that catches most of
  these before users see them
- [[Support Report Flow]] — what to do when a user hits an issue not
  on this list
- [[AI Wizard]] — the readiness states the WebUI surfaces
- [[Service Order]] — boot dependency map for log triage
- [[GPU Capability Matrix]] — when "auto-enable" doesn't fire
""",
        "Process Playbooks/Headless QA Loop.md": """---
tags: [playbook, setup, qa]
---
# Headless QA Loop

Automates the [[Pilot Test Checklist]] on a rented cloud GPU desktop
(e.g. NVIDIA CloudMatch Beta / GeForce NOW Enterprise private tile,
RunPod, Lambda) so a "did nvHive install clean?" answer comes back
without any manual clicking. Run before tagging a release.

The loop:

1. Launch a dedicated, isolated Chrome that the test harness owns —
   never the user's everyday browser. This avoids tab-routing ambiguity
   and lets the harness drive input over the Chrome DevTools Protocol
   (CDP), where events are marked as trusted by the renderer.
2. Open the cloud-desktop deep-link, dismiss any session-ready scrim,
   wait for the streamed Ubuntu desktop.
3. Send the install one-liner into the streamed Konsole via CDP key
   events.
4. Wait for `nvh selfcheck` to report green; screenshot the [[AI Wizard]]
   surface; diff against the recorded baseline.
5. If anything regressed, file the screenshot + the failing step as a
   support report — same shape as [[Support Report Flow]].

## Why this is its own playbook

Driving cloud-desktop streamers via synthesized input is fragile in
specific ways that informed the harness design:

- **Tab visibility gating.** The streamer's "Your rig is ready" scrim
  refuses to dismiss while Chrome's `document.visibilityState` is
  `hidden`. A tab that is "active within Chrome" is not enough; the
  Chrome window itself must be the foreground OS window. CDP input
  dispatch bypasses this because it talks directly to the renderer.
- **Session bumping.** Cloud-desktop providers enforce a single live
  stream per account. A second tab opening the same rig will bump the
  first. The dedicated Chrome the harness owns must be the *only*
  process holding a session, so close stray tabs first.
- **WebRTC streamer input mode.** Some providers (GeForce NOW
  specifically) gate keyboard/mouse passthrough on a real
  "session-active" handshake — letting the rig-ready scrim auto-dismiss
  or clicking it via the harness is required, and the streamer also
  drops input if the `<video>` element isn't playing. The harness must
  keep the video playing and refresh activity to prevent the
  provider's idle-timeout from ending the session.

## Provider compatibility

- **RunPod / Lambda / Vast / Paperspace**: SSH + web terminal. The
  install one-liner runs directly. Simplest path for unattended CI.
- **GeForce NOW (CloudMatch Beta)**: viable via dedicated Chrome +
  `--remote-debugging-port` + CDP. Drive deeplink → tile → PLAY,
  background keepalive (`mouseMoved` every ~60s), CDP keystrokes into
  the streamed Konsole. The right channel when QA also needs to
  validate the streamed-desktop UX (auto-launched browser, KDE Plasma
  interactions).

## Running the loop

The Operator harness lives in `tools/`. The current entry points are
`tools/operator_install.sh` for one-shot install and the SSH-backed
QA driver for the actual loop.

## How NVIDIA Nemotron Omni gets installed

The Ollama library doesn't publish `nemotron-omni` or
`nemotron-3-nano-omni` yet (verified 2026-05-17 and 2026-05-20). The
install handles this in two layers:

1. **HuggingFace → Ollama Modelfile bootstrap.** When `ollama pull
   nemotron-omni` 404s, the installer downloads the official GGUF +
   vision projector from the `ggml-org/NVIDIA-Nemotron-3-Nano-Omni`
   HuggingFace repo and registers it locally with an Ollama Modelfile
   (`ollama create`). Picks the Q8_0 quant on 40 GB+ rigs, Q4_K_M on
   24-40 GB. After this, `ollama list` shows `nemotron-omni` as a
   local tag and the Wizard uses it like any other Ollama model.

2. **Soft fallback chain.** If the HF bootstrap also fails — too
   little disk, opted out via `NVH_INSTALL_MODEL_DOWNLOAD=0`, ollama
   create rejects the Modelfile, etc. — the installer walks down a
   chain of vision-capable models that **do** exist in the Ollama
   library: `llama3.2-vision` → `minicpm-v` → `moondream`. The Wizard
   stays multimodal even on the smallest GPUs.

Skip the Omni download with `NVH_INSTALL_MODEL_DOWNLOAD=0` before
running the installer if you'd rather just take the Path 2 fallback
(useful on slow connections or when testing the install path itself).

## Related

- [[Pilot Test Checklist]] — the manual variant, useful when the
  harness can't reach the rig
- [[Support Report Flow]] — what to do when a step fails
- [[AI Wizard]] — the readiness surface the harness diffs against
- [[Service Order]] — what the harness is verifying came up in order
- [[GPU Capability Matrix]] — which capabilities the rig should
  auto-enable based on VRAM
""",
        "Decisions/Multi-Expert Council.md": """---
tags: [decision, wizard, product]
---
# Multi-Expert Council

The [[AI Wizard]] handles the steady-state product surface. The
**council** is the escape hatch when one persona isn't the right shape
for the question — e.g. legal + product + engineering tradeoffs at the
same time.

Council mode is fully built and shipping today (`nvh/core/council.py`,
`nvh/core/agents.py`). Until recently it was hidden behind the API
surface; the WizardChat composer now offers a "Convene council" link
that deep-links into the dedicated `/council` page with the current
draft pre-filled as the prompt.

## When to convene

Convene a council when **at least two** of these apply:

- The question crosses domains (legal × engineering, marketing ×
  technical, customer × founder).
- The answer is a tradeoff, not a fact — and the user needs to *see*
  the tradeoff debated rather than handed a single recommendation.
- The downside of the wrong answer is large (naming, licensing,
  refactor scope, channel partner pick).
- The user explicitly asks for "another opinion" or "what would <X>
  say about this."

Skip the council for ordinary "fix this bug" / "what does this command
do" turns — the Wizard answers those faster.

## Available cabinets

11 cabinets ship by default (e.g. `legal`, `marketing-and-customer`,
`engineering-and-product`, `student-feedback`). Each cabinet is a
named subset of the 23 personas, with a strategy:

- `weighted_consensus` — personas weighted by how much they know the
  domain; produces a single synthesized answer.
- `majority_vote` — every persona answers, vote tallied.
- `best_of` — every persona answers, the Wizard picks the most useful.

## API surfaces

- `POST /v1/council` — synchronous (returns the full transcript).
- `WS /v1/council/stream` — token-streaming each persona.
- `/council` page in the WebUI — wraps the WS endpoint with a UI for
  picking cabinet + strategy + watching the debate play out.

## Related

- [[AI Wizard]] — the default single-persona surface this escalates from
- [[Product Direction]] — why having a "convene experts" gesture matters
- [[Product Conflicts]] — the kind of tradeoffs councils exist to surface
- [[Pilot Test Checklist]] — councils have their own readiness signals
""",
        "Decisions/GPU Capability Matrix.md": """---
tags: [decision, setup, product]
---
# GPU Capability Matrix

Different rigs run different capabilities. The installer probes VRAM
once and picks the right defaults so users on a 6 GB laptop don't get
a 40 GB ComfyUI download and users on a 48 GB RTX 6000 Ada don't end up
on `moondream`. The canonical matrix lives in
`docs/GPU_TIER_MATRIX.md` — when in doubt, that file wins over this
note. This note explains the *intent* the matrix encodes so the
[[AI Wizard]] and [[Multi-Expert Council]] can reason about it.

## VRAM tiers and the default Wizard model

Every install lands a vision-capable chat model — that part needs no
opt-in:

- **40 GB+**: `nemotron-omni` (NVIDIA Nemotron Omni, multimodal flagship).
- **24–40 GB**: `nemotron-3-nano-omni` (30B MoE, 3B active, multimodal).
- **16–24 GB**: `llama3.2-vision` (11B, multimodal).
- **12–16 GB**: `minicpm-v` (small multimodal).
- **< 12 GB / CPU-only**: `moondream` (tiny multimodal, ~2 GB; runs
  usefully on CPU). The Wizard works via cloud providers for anything
  heavier.

## What stages beyond chat (opt-in only)

Image, video, speech, and music packs are **never staged by default**
— they're gated behind `NVH_INSTALL_FULL_CAPABILITY=1` because they
cumulatively download tens of gigabytes. With the knob set, staging is
strictly VRAM-tiered:

- **8 GB+**: image generation starter (ComfyUI + Z-Image-Turbo).
- **12 GB+**: + image editing (Qwen Image Edit 2509).
- **16 GB+**: + image control (FLUX.1 ControlNet).
- **24 GB+**: + video (Wan 2.2 5B), local speech (WhisperX +
  faster-whisper via the music-producer-lab pack), music (ACE-Step).
- **40 GB+**: + video pro (Wan 2.2 14B i2v).

There is no CPU-only override: the staging step only runs when a GPU
is detected, and a small GPU stages only what its tier qualifies for.
There is also no Piper TTS pack and no local TTS gated by VRAM — the
default voice path is Edge TTS (cloud, free) for TTS and Groq Whisper
(cloud, free with a key) for STT, which work on any rig including
no-GPU ones.

## What "Omni" means here (and what it doesn't)

NVIDIA Nemotron Omni is a **multimodal LLM** — vision + text in, text
out. It is *not* an image generator (use ComfyUI + diffusion for that)
and it is *not* a speech model (use WhisperX / XTTS for that).
Image gen and speech are separately VRAM-gated capabilities, not part
of "Omni."

## How the staging marker works today

With `NVH_INSTALL_FULL_CAPABILITY=1`, the installer writes
`auto-enable.json` under `$NVH_HOME/state/capability/` recording which
capabilities and pack ids the rig qualifies for. The marker is written
for a future WebUI/Wizard consumer — **nothing reads it yet**, so the
knob alone has no runtime effect. The companion
`NVH_INSTALL_FULL_CAPABILITY_DOWNLOAD=1` is what actually pulls the
qualifying packs inline at install time (meant for headless cloud
images that won't have a browser later).

## Related

- [[AI Wizard]] — uses this matrix to decide which models to recommend
- [[Multi-Expert Council]] — falls back to text-only mode on small rigs
- [[Headless QA Loop]] — checks that auto-enable matches expected tier
- [[Common Install Issues]] — falling-back-to-cloud diagnostics
- [[Service Order]] — Ollama health-wait depends on the model size we
  asked it to load
""",
        "Process Playbooks/Service Order.md": """---
tags: [playbook, setup]
---
# Service Order

The install + launch path runs three services plus a final end-to-end
check, in a specific dependency order. When something looks broken,
this note tells you which step *was* supposed to come up first and
where to look in the logs. Canonical contract lives in
`docs/SERVICE_ORDER.md`; this note is the on-rig mirror so
[[AI Wizard]] support reports can deep-link to it.

## Boot sequence

1. **Ollama** (`http://localhost:11434`) — local model server.
   Health-checked by polling `GET /api/tags` until 200 or
   `NVH_OLLAMA_BOOT_TIMEOUT` (default 15s) expires.
2. **nvHive API** (`http://localhost:8000`) — FastAPI server.
   Health-checked by `GET /v1/health` returning 200 *and*
   `engine_initialized: true`. A live TCP port is not enough — a stale
   process accepting connections but failing to init the engine is the
   exact failure mode [[Common Install Issues]] §1 covers.
3. **Next.js WebUI** (`http://localhost:3000`, cascade 3001/3002/8080).
   Polls the API health endpoint before showing panels; until then,
   surfaces the amber "Starting up…" banner from
   `web/components/ApiHealthBanner.tsx`.
4. **Wizard smoke test** — `POST /v1/wizard/chat` with a tiny real
   question (90s budget; cold first runs can spend 30-60s warming
   imports). `mode: "llm"` is fully healthy; a `deterministic` mode
   means **degraded** — the Wizard answers via its fallback because no
   local LLM is loaded yet. The browser only opens after this step:
   green and degraded both open it (degraded shows a yellow row with
   the fallback reason), hard failures don't.

## Env knobs

- `NVH_OLLAMA_BOOT_TIMEOUT=30` — wait longer for Ollama on slow rigs.
- `NVH_API_BOOT_TIMEOUT=45` — wait longer for the FastAPI engine to
  initialize (cold imports + model warm-up can run 20-30s).
- `NVH_WEBUI_BOOT_TIMEOUT=30` — Next.js dev server bind window.
- `NVH_OLLAMA_PRELOAD=0` — skip the post-boot warmup of the default
  model (on by default so the first chat turn isn't a cold load).
- `NVH_PORTS_TO_CHECK="3000 3001 3002 8000 11434"` — ports the
  install-time conflict detector probes.
- `NVH_PORT_CONFLICT_KILL_FOREIGN=1` — let the installer kill a
  foreign process holding one of the stack ports. Off by default to
  avoid silently stomping the user's own work.

## Health invariants the helpers check

`nvh services` (and the `webui()` command which delegates to it) uses
these module-level helpers in `nvh/cli/services.py`:

- `api_healthy(port)` → HTTP probe `/v1/health`, parse JSON, require
  `engine_initialized: true`.
- `ollama_healthy(port)` → HTTP probe `/api/tags`, require 200.
- `webui_port_listening(port)` → TCP `connect()` only (the Next.js
  dev server doesn't expose a JSON health endpoint).
- `wizard_smoke_test(port)` → POST `/v1/wizard/chat`, require a
  non-empty answer; reports `mode` so degraded is visible.
- `kill_stale_api(port)` → `fuser -k <port>/tcp` on Linux,
  `lsof + kill -TERM` on macOS.

## Recovery commands

- `nvh services status` — live table of all three services.
- `nvh services start` — boot everything in order; browser opens only
  when every gate is green.
- `nvh services restart` — recycle API + WebUI (Ollama keeps its warm
  model cache).
- `nvh services stop` — stop WebUI + API; add `--ollama` to stop
  Ollama too.
- `nvh services smoke-test` — re-run the end-to-end Wizard check on
  its own (default 45s budget, `--timeout` to extend).

## What goes wrong + where to look

| Symptom | Most likely cause | Log path |
| --- | --- | --- |
| Red "API offline" banner | Stale `nvh serve` engine failed init | `$NVH_HOME/logs/api-server.log` |
| Amber "Starting up…" lingers >20s | API still warming the engine | `$NVH_HOME/logs/api-server.log` |
| Wizard falls back to cloud | Ollama bind raced timeout | `$NVH_HOME/logs/ollama.log` |
| Smoke test degraded (deterministic) | No local LLM loaded yet | finish the download from /setup, then `nvh services smoke-test` |
| Install aborts "FOREIGN port" | Non-nvh process on stack port | run `lsof -nP -iTCP:<port>` |
| WebUI never opens | Browser-open chain failed | `$NVH_HOME/logs/webui.log` |

## Related

- [[Common Install Issues]] — symptom-driven entry to the same content
- [[AI Wizard]] — owner of the readiness surface
- [[Headless QA Loop]] — automates verification this order holds
- [[GPU Capability Matrix]] — affects Ollama health-wait duration
""",
    }

    for relative, content in files.items():
        path = vault / relative
        if _write_if_missing(path, content):
            seeded[relative] = str(path)

    obsidian_dir = vault / ".obsidian"
    obsidian_dir.mkdir(parents=True, exist_ok=True)
    app_json = obsidian_dir / "app.json"
    if _write_if_missing(
        app_json,
        # alwaysUpdateLinks=true keeps wikilinks pointing at the right
        # note when files are renamed; useLegacyToggle=false enables the
        # tag-color cluster panel by default; newLinkFormat="shortest"
        # keeps [[Wikilinks]] compact.
        json.dumps(
            {
                "alwaysUpdateLinks": True,
                "useLegacyToggle": False,
                "newLinkFormat": "shortest",
                "showLineNumber": True,
            },
            indent=2,
        ),
    ):
        seeded[".obsidian/app.json"] = str(app_json)
    appearance_json = obsidian_dir / "appearance.json"
    if _write_if_missing(
        appearance_json,
        json.dumps({"accentColor": "#76B900", "theme": "obsidian"}, indent=2),
    ):
        seeded[".obsidian/appearance.json"] = str(appearance_json)
    plugins_json = obsidian_dir / "community-plugins.json"
    if _write_if_missing(plugins_json, "[]"):
        seeded[".obsidian/community-plugins.json"] = str(plugins_json)

    # Graph view defaults — coloring nodes by the YAML `tags` we seeded
    # so the cluster mesh is visible immediately on first open. Without
    # this file, Obsidian's Graph view defaults to all-grey-dots which
    # hides the structure we just wired in.
    graph_json = obsidian_dir / "graph.json"
    if _write_if_missing(
        graph_json,
        json.dumps(
            {
                "collapse-filter": True,
                "search": "",
                "showTags": True,
                "showAttachments": False,
                "hideUnresolved": False,
                "showOrphans": True,
                "collapse-color-groups": False,
                "colorGroups": [
                    {"query": "tag:#wizard", "color": {"a": 1, "rgb": 7777124}},
                    {"query": "tag:#decision", "color": {"a": 1, "rgb": 14774017}},
                    {"query": "tag:#conflict", "color": {"a": 1, "rgb": 14430245}},
                    {"query": "tag:#playbook", "color": {"a": 1, "rgb": 4231423}},
                    {"query": "tag:#moc", "color": {"a": 1, "rgb": 16776960}},
                ],
                "collapse-display": False,
                "showArrow": False,
                "textFadeMultiplier": 0,
                "nodeSizeMultiplier": 1.2,
                "lineSizeMultiplier": 1,
                "collapse-forces": False,
                "centerStrength": 0.5,
                "repelStrength": 10,
                "linkStrength": 1,
                "linkDistance": 250,
                "scale": 1,
                "close": True,
            },
            indent=2,
        ),
    ):
        seeded[".obsidian/graph.json"] = str(graph_json)

    return seeded


def _conflicts_markdown() -> str:
    rows = "\n".join(f"- **{item['title']}**: {item['summary']}" for item in KNOWN_CONFLICTS)
    return f"""---
tags: [conflict, decision]
---
# Product Conflicts

These are intentional tradeoffs the [[AI Wizard]] should explain
instead of hiding. They link back to [[Product Direction]] for the
underlying bets.

{rows}

## Related

- [[AI Wizard]] — the agent that surfaces these to the user
- [[Product Direction]] — the bets these tradeoffs come from
- [[Support Report Flow]] — when a "this is intentional" comes up
"""


def init_vault(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Create the rootless Markdown vault and seed product memory."""
    storage = ensure_storage(home_dir, min_free_gb=0, activate=True)
    layout = storage.layout
    vault = layout.home / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    for dirname in VAULT_DIRS:
        (vault / dirname).mkdir(parents=True, exist_ok=True)

    seeded = _seed_notes(vault, layout.home)
    append_path = vault / "System" / "Boot Health.md"
    append_path.write_text(
        f"""# Boot Health

Last refreshed: {_now()}

- NVH_HOME: `{layout.home}`
- Python: `{platform.python_version()}`
- Platform: `{platform.platform()}`
- Machine: `{platform.machine()}`

Use AI Wizard support reports for redacted logs, receipts, and compatibility checks.
""",
        encoding="utf-8",
    )

    return {
        "initialized": True,
        "vault_dir": str(vault),
        "home_dir": str(layout.home),
        "seeded_files": seeded,
        "markdown_files": _markdown_count(vault),
        "conflicts": KNOWN_CONFLICTS,
        "obsidian": obsidian_status(home_dir=layout.home),
    }


def vault_status(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Return vault status without requiring Obsidian."""
    layout = storage_layout(home_dir)
    vault = layout.home / "vault"
    initialized = vault.exists() and (vault / "README.md").exists()
    return {
        "initialized": initialized,
        "vault_dir": str(vault),
        "home_dir": str(layout.home),
        "markdown_files": _markdown_count(vault),
        "directories": {name: str(vault / name) for name in VAULT_DIRS},
        "conflicts": KNOWN_CONFLICTS,
        "obsidian": obsidian_status(home_dir=layout.home),
    }


def append_vault_memory(
    title: str,
    body: str,
    *,
    category: str = "Wizard Memory",
    tags: list[str] | None = None,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Append a Markdown memory note under the vault."""
    status = init_vault(home_dir)
    vault = Path(status["vault_dir"])
    safe_category = category if category in VAULT_DIRS else "Wizard Memory"
    target_dir = vault / safe_category
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = _now()
    filename = f"{timestamp.replace(':', '').replace('+', 'Z')}-{_slug(title)}.md"
    path = target_dir / filename
    tag_line = " ".join(f"#{_slug(tag).lower()}" for tag in (tags or []) if tag.strip())
    path.write_text(
        f"""# {title.strip()}

Created: {timestamp}
Category: {safe_category}
Tags: {tag_line or "none"}

{body.strip()}
""",
        encoding="utf-8",
    )
    return {
        "saved": True,
        "path": str(path),
        "category": safe_category,
        "title": title.strip(),
        "vault_dir": str(vault),
    }


def _platform_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x86_64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return machine


def _obsidian_paths(home_dir: str | Path | None = None) -> dict[str, Path]:
    layout = storage_layout(home_dir)
    root = layout.apps_dir / "obsidian"
    return {
        "root": root,
        "appimage": root / "Obsidian.AppImage",
        "launcher": layout.bin_dir / "nvhive-vault",
        "vault": layout.home / "vault",
    }


def obsidian_status(home_dir: str | Path | None = None) -> dict[str, Any]:
    paths = _obsidian_paths(home_dir)
    system_obsidian = shutil.which("obsidian")
    installed = paths["appimage"].exists() or bool(system_obsidian)
    arch = _platform_arch()
    linux = platform.system().lower() == "linux"
    return {
        "installed": installed,
        "rootless_app": paths["appimage"].exists(),
        "app_path": str(paths["appimage"]) if paths["appimage"].exists() else system_obsidian,
        "launcher_path": str(paths["launcher"]),
        "open_command": (
            f'"{paths["launcher"]}"'
            if paths["launcher"].exists()
            else f'open "{paths["vault"]}" in Obsidian'
        ),
        "installable": linux and arch == "x86_64",
        "reason": (
            ""
            if linux and arch == "x86_64"
            else "Rootless Obsidian auto-install currently targets Linux x86_64 AppImage builds."
        ),
        "download_url": OBSIDIAN_DOWNLOAD_PAGE,
        "vault_dir": str(paths["vault"]),
    }


def _write_obsidian_launcher(home_dir: str | Path | None = None) -> Path:
    layout = storage_layout(home_dir)
    paths = _obsidian_paths(layout.home)
    paths["launcher"].parent.mkdir(parents=True, exist_ok=True)
    script = f"""#!/usr/bin/env bash
set -e
export NVH_HOME="${{NVH_HOME:-{layout.home}}}"
VAULT="${{NVH_HOME}}/vault"
APP="${{NVH_HOME}}/apps/obsidian/Obsidian.AppImage"
if [ -x "$APP" ]; then
  exec "$APP" "$VAULT"
fi
if command -v obsidian >/dev/null 2>&1; then
  exec obsidian "$VAULT"
fi
echo "Obsidian is not installed. Your nvHive Vault is at: $VAULT"
exit 1
"""
    paths["launcher"].write_text(script, encoding="utf-8", newline="\n")
    paths["launcher"].chmod(
        paths["launcher"].stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return paths["launcher"]


def _latest_obsidian_appimage_asset() -> dict[str, Any]:
    req = urllib.request.Request(
        OBSIDIAN_RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "nvhive-rootless-vault"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))
    assets = release.get("assets") or []
    for asset in assets:
        name = str(asset.get("name") or "")
        if (
            name.endswith(".AppImage")
            and "arm64" not in name.lower()
            and "aarch64" not in name.lower()
        ):
            return asset
    raise RuntimeError("No Linux x86_64 Obsidian AppImage asset found in the latest release.")


def install_obsidian(
    home_dir: str | Path | None = None,
    *,
    force_update: bool = False,
) -> dict[str, Any]:
    """Install Obsidian AppImage under NVH_HOME/apps when supported."""
    init_vault(home_dir)
    layout = storage_layout(home_dir)
    paths = _obsidian_paths(layout.home)
    paths["root"].mkdir(parents=True, exist_ok=True)

    status = obsidian_status(home_dir=layout.home)
    launcher = _write_obsidian_launcher(layout.home)
    if paths["appimage"].exists() and not force_update:
        status.update({
            "installed": True,
            "rootless_app": True,
            "app_path": str(paths["appimage"]),
            "launcher_path": str(launcher),
            "message": "Obsidian is already installed in NVH_HOME.",
        })
        return status

    if not status["installable"]:
        status.update({
            "installed": paths["appimage"].exists() or bool(shutil.which("obsidian")),
            "launcher_path": str(launcher),
            "message": (
                status["reason"] or "Obsidian auto-install is not available on this platform."
            ),
        })
        return status

    asset = _latest_obsidian_appimage_asset()
    url = asset.get("browser_download_url")
    if not url:
        raise RuntimeError("Latest Obsidian release asset did not include a download URL.")

    tmp_path = paths["root"] / "Obsidian.AppImage.download"
    urllib.request.urlretrieve(url, tmp_path)
    tmp_path.replace(paths["appimage"])
    paths["appimage"].chmod(
        paths["appimage"].stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    return {
        "installed": True,
        "rootless_app": True,
        "app_path": str(paths["appimage"]),
        "launcher_path": str(launcher),
        "open_command": f'"{launcher}"',
        "installable": True,
        "reason": "",
        "download_url": url,
        "vault_dir": str(paths["vault"]),
        "message": (
            "Obsidian installed rootlessly. If the VM lacks FUSE, open the vault folder "
            "in any Markdown editor or use Obsidian from the OS image."
        ),
    }
