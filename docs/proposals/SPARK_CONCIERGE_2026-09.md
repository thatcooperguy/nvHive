# nvHive on Spark: the on-device helper

**Status:** proposal, owner-directed 2026-09-02. Supersedes the rented-desktop
framing in [SIMPLIFICATION_PLAN_2026-09.md](SIMPLIFICATION_PLAN_2026-09.md)
for everything Wizard-facing; the subtract/refresh mechanics in that plan
still apply. Tracking: #136 (platform) and #137 (concierge).

## 1. The ask

The owner's direction, paraphrased:

- The Wizard must *just work*. A Spark owner never picks an agent or an
  "expertise". Specialists exist, but under the hood, chosen per turn.
- Think of it as the old Office paper clip: a simple, always-present,
  sprite-based NVIDIA avatar that reacts to what the helper is doing.
- Deep-dive which specialists belong under the hood. Home Assistant (the
  smart-home platform) was named as one.
- Spark owners often have root. The helper must be able to guide device
  settings and install the applications people buy a Spark for, safely.
- The result is "a true helper LLM that runs on the device".

Target hardware: **DGX Spark** (GB10 Grace Blackwell, 128 GB unified
LPDDR5x at ~273 GB/s, aarch64, DGX OS) today; **RTX Spark** (same class,
Windows on Arm laptops and compact desktops from ASUS, Dell, HP, Lenovo,
Surface and MSI) when it ships this fall.

## 2. What the code does today (verified 2026-09-02)

Three read-only code maps grounded this plan. The load-bearing facts:

**Agent selection is a dropdown.** `POST /v1/wizard/chat/stream` takes a
`profile` string; `WizardChat.tsx` defaults it to `"wizard"` and offers a
flat `<select>` of 106 profiles (6 core, 100 library, plus custom) in 38
category groups. Nothing selects a profile from the question. The only
classifier on the path, `TaskClassifier` in `nvh/core/router.py`, picks a
*provider and model*, not a persona. The council's trigger-based persona
matcher (`nvh/core/agents.py`) is the right pattern but is never reachable
from the Wizard.

**Profile fields are declared but ignored.** `tools_allowed` is not
enforced anywhere (the prompt lists every tool and `_run_auto_tool` runs
any of them); `temperature` and `max_tokens` are overridden by engine
defaults; `max_cost_usd_per_turn` is enforced only on the non-streaming
path; `confirm_required` is not emitted when the first iteration produces a
confirm-class call. Fixing these is a prerequisite for hidden specialists,
because a hidden specialist is exactly "a profile whose tool whitelist and
knobs actually bind".

**The Wizard never learns the machine.** `wizard_context()` builds the GPU
block by checking `isinstance(primary, dict)` on a `GPUInfo` dataclass, so
the model always sees `name: null, vram_gb: null`. No architecture, distro,
kernel, unified-memory flag, root/sudo fact or device class reaches the
prompt. A DGX Spark is misclassified as a cloud desktop by the
`board_vendor == nvidia` heuristic, `GB10` is not recognised by name
(compute capability `(0,0)`, architecture "Unknown"), and unified memory is
double-counted as VRAM plus CPU offload in `recommend_models()`.

**Two tool registries, no bridge.** `nvh/core/tools.py` (CLI/agent loop:
shell, run_code, files, system, browser, vision) and
`nvh/integrations/wizard/tools.py` (WebUI: diagnose, refresh_models,
repair_workspace, keys, RAG, web_search, MCP). Only the second reaches the
WebUI, and it already renders a confirm card for any `confirm`-class tool
without UI changes. Studio packs (18) are rootless installers with
receipts, repair and uninstall plans. There is no smart-home code, no MCP
preset catalog, no privileged-action tier.

**No mascot, no animation library.** One `AgentAvatar` component (20–56 px,
monogram fallback), six inline SVG monograms, `web/public` holds only brand
icons, and motion is hand-written CSS. `LayoutShell.tsx` already hosts
fixed overlays on every route including first-run `/setup`.

**Release matrix is x86_64-only.** `release.yml` builds `nvh-linux-x86_64`,
`start-linux.sh` hard-codes that URL, CI tests `ubuntu-latest` only.
`install.sh` already maps `aarch64` to the arm64 Ollama tarball;
`install.ps1` has no architecture check at all.

## 3. Design

### 3.1 One Wizard, hidden specialists

`profile` becomes optional and defaults to **`auto`**. A new
`nvh/integrations/wizard/concierge.py` selects a specialist per turn:

1. **Deterministic triggers first** (the `PersonaTemplate.triggers`
   pattern from `nvh/core/agents.py`): keyword and regex hits on the
   question, plus *state* triggers from `wizard_context()` and findings
   (failed job → install medic; `gpu-missing` → rig doctor; no models →
   model sommelier; first run on `dgx-spark` → setup concierge).
2. **`TaskClassifier` as tie-breaker** for the residue (coding → coder,
   research → researcher, etc.).
3. **Default** is the general Wizard persona.

The chosen specialist contributes its system prompt, tool whitelist,
temperature and cost ceiling; the *user sees one assistant*. The response
carries `used_profile` so the UI can show a small "helped by: Rig Doctor"
caption and power users can pin a profile via `/agent <name>` in the
composer. `/agents` becomes a browse-only page; the picker leaves the
composer.

Reuse before invention: the Ops category already holds `install-medic`,
`gpu-triage`, `model-librarian`, `vram-planner`, `provider-keysmith`,
`latency-tuner`, `finetune-advisor`; `deep-reviewer`, `deep-researcher`,
`code-tutor`, `shell-teacher`, `doc-qa` cover coding, research, learning
and documents. New profiles are added only where a tool set is new.

### 3.2 The roster under the hood

| Specialist | Exists? | Tools it binds | Trigger examples |
|---|---|---|---|
| **Setup concierge** — first-run guide, one step at a time | `setup-concierge` (shipped) | diagnose, refresh_models, repair_workspace, validate_provider_key, save_provider_key, rag_ask_vault | first launch, "how do I get started", `platform.device_class` + no models |
| **Rig doctor** | `gpu-triage` + `install-medic` | diagnose, repair_workspace, refresh_models, rag_ask_vault | errors pasted, "not working", failed job in context |
| **Model sommelier** — what fits, MoE-first on unified memory | `model-sommelier` (shipped); `vram-planner` keeps sizing arithmetic, `model-librarian` the shelf | refresh_models, diagnose, web_search, rag_ask_vault | "which model", "fits", "best model for coding", "MoE vs dense" |
| **Device settings** — DGX OS / Windows settings with sudo | new, privileged | `system_settings_*` (§3.4) | "enable ssh", "auto login", "update", "firewall", "sleep" |
| **App installer** — Spark playbooks as packs | new, privileged | `install_pack`, `playbook_*` (§3.5) | "install docker", "comfyui", "jupyter", "vllm", "open webui" |
| **Home Assistant operator** | new | `home_assistant_*` (§3.6) | "lights", "thermostat", entity ids, "automation" |
| **Coding pair** | `deep-reviewer`, `backend-implementer` | run_code/shell via sandbox (0.44) | code blocks, stack traces |
| **Researcher** | `deep-researcher`, `fact-checker` | web_search, rag_ask | "look up", "latest", URLs |
| **Notes and vault** | `vault-rag`, `daily-notes-coach` | rag_ask_vault, rag_ingest | "my notes", "remember", "what did we decide" |
| **Media librarian** — photos, OCR, captions with local vision | new (wraps `analyze_image`, `read_text_from_image`) | vision tools bridged into the Wizard registry | image attached, "what's in this", "read this receipt" |
| **Tutor** | `code-tutor`, `science-explainer`, `math-stepper` | none | "explain", "teach me", "why" |

Candidates considered and parked: finance and health helpers (the product
should not give personalised financial or medical advice), a calendar/email
agent (needs OAuth to third-party accounts; out of the single-VM envelope
for now).

### 3.3 The mascot

A `Mascot` component mounted in both `LayoutShell` branches, bottom-right,
driven by a tiny store fed from the Wizard SSE stream:

| Event | State |
|---|---|
| `iteration` | thinking |
| `tool_call`, `tool_result` | working |
| `confirm_required` | asking (bubble: "I need a yes for this") |
| `done` | happy → idle |
| `error` | error → idle |
| 90 s idle | sleeping |

Sprite sheet + JSON manifest in `web/public/mascot/`, CSS `steps()`
animation, `prefers-reduced-motion` honoured, hide/show persisted, tips
(top diagnostic finding, once per finding per session) in a dismissible
bubble. **The shipped sprite is a neutral placeholder** (a hexagon-headed
"hive spirit" in nvHive green). The owner asked for Jensen Huang as the
mascot; a living person's likeness needs his and NVIDIA's sign-off, and the
repository's NOTICE and EULA state nvHive is independent and not endorsed
by NVIDIA. The sheet is a file swap once cleared.

### 3.4 Privileged actions (sudo) — the new tier

Owned hardware changes the envelope: "everything rootless" becomes
**rootless by default, privileged with approval when the owner has it**.

- `PlatformFacts.can_sudo` is probed with `sudo -n true` only; nvHive
  never prompts for, sees or stores a password. If a password is required,
  the helper hands the exact command to a terminal card the user runs
  themselves.
- New Wizard tool class **`privileged`** (renders a red approval card, no
  auto-approve possible, disabled entirely when `NVH_ALLOW_PRIVILEGED=0`):
  `system_settings_get` (auto: read-only facts), `system_settings_plan`
  (auto: the exact commands, a dry run), `system_settings_apply`
  (privileged), `apt_install` / `snap_install` (privileged),
  `service_enable` (privileged).
- Every privileged apply writes a vault note under `Decisions/` with the
  commands run and their output, and an install receipt with an uninstall
  plan where one exists.
- The deny list is fixed: no user/password changes, no disk formatting,
  no firewall-off, no driver removal, no `rm -rf` outside `NVH_HOME`.

### 3.5 Spark playbooks as packs

NVIDIA publishes DGX Spark playbooks (the canonical list of what owners
install: containers, inference servers, notebooks, ComfyUI, fine-tuning,
two-node clustering). Each becomes a `StudioPack` with `install_kind`
`playbook`, a `requires_sudo` flag, and a receipt, so the helper can say
"you're on a DGX Spark with 128 GB; here are the six things owners set up
first" and do them one confirm card at a time. First tier (from §5):
Ollama (native), Open WebUI, VS Code remote, ComfyUI, vLLM, llama.cpp,
LM Studio, NIM, Unsloth, Tailscale, Connect Two Sparks, NemoClaw. Each
pack records whether it needs sudo, which ports it binds, and the
validated update channel to use, so the helper never suggests a bare
`apt upgrade` on DGX OS.

### 3.6 Home Assistant

`nvh/integrations/home_assistant.py` (httpx client over the HA REST API,
long-lived token from `HASS_URL`/`HASS_TOKEN`) exposed as five Wizard
tools: `home_assistant_status`, `_entities`, `_state`, `_services` (auto)
and `home_assistant_call` (confirm). A `home-assistant` library profile
under a new **Smart Home** category binds exactly those tools. Admin
domains (`hassio`, `shell_command`, `python_script`, restart/stop) are
refused unless `NVH_HASS_ALLOW_ADMIN=1`. Unconfigured state returns a hint
on creating a token; no network call is made.

## 4. Phases

| Phase | Release | Contents |
|---|---|---|
| **0** groundwork | 0.43 | profile fields enforced; platform facts (`device_class`, unified memory, sudo) in the prompt; GB10 recognised; DGX Spark not a "cloud desktop"; `nvh-linux-arm64` release + arm64 CI + arch-aware `start-linux.sh`; mascot with placeholder sprite; Home Assistant tools + profile |
| **1** concierge | 0.43 | `profile=auto` routing in `concierge.py`; picker leaves the composer; `used_profile` caption; setup concierge and model sommelier profiles; unified-memory-aware `LOCAL_MODEL_TIERS` (#136 Phase B) |
| **2** privileged | 0.44 | `privileged` tool class + approval card; `system_settings_*`; playbook packs with `requires_sudo`; vault audit; `NVH_ALLOW_PRIVILEGED` |
| **3** bridge | 0.44 | vision and sandbox tools reach the Wizard registry (the one-`Tool` item in the simplification plan); media librarian and coding pair become real |
| **4** RTX Spark | when hardware ships | `nvh-windows-arm64.exe`, arch-aware `install.ps1`, Windows unified-memory probe, `rtx-spark` device class validated |

## 5. Who buys a Spark and what they need in week one

From the 2026-09-02 web research (sources are attached to #137).

**Buyers.** NVIDIA positions DGX Spark for developers, researchers, data
scientists and AI engineers; every major review reaches the same verdict:
a CUDA-native 128 GB *development* box, not a throughput box. Real buyers:
AI developers replacing a $200–1,500/month API bill, researchers and
grad students who want to skip the cluster queue (NYU, Harvard Kempner,
ASU robotics, ISTA), fine-tuners of sub-8B models, robotics and edge
developers who train on Spark and deploy to Jetson, privacy-bound and
regulated teams, and hobbyists who wanted CUDA rather than a Mac Studio.
The OEM twins (ASUS Ascent GX10, Dell Pro Max GB10, HP ZGX Nano, Lenovo
ThinkStation PGX, MSI EdgeXpert, Acer Veriton GN100) share the SoC; the
choice is price, procurement and noise. RTX Spark is positioned at
creators, developers and power users (Copilot+ class, ~$2,000–3,000
laptops from eight OEMs plus a Surface "Dev Box" desktop); the press
expects developers and early adopters, not mainstream consumers.

**The first week, in order.** First boot and OTA update (headless boxes
broadcast a `spark-xxxx` hotspot); NVIDIA Sync on the laptop and the DGX
Dashboard for monitoring, updates and JupyterLab; SSH keys, Tailscale and
a firewall scoped to it; Docker sanity (the user is not in the `docker`
group by default; the NVIDIA runtime sometimes needs configuring); NGC
login; then the first model, almost always Ollama plus Open WebUI. Next
come a Python environment that works on `sm_121`/aarch64 (`uv`, cu130
wheel indexes, no flash-attn), ComfyUI, an inference server (llama.cpp,
vLLM, SGLang, LM Studio), fine-tuning (Unsloth, LLaMA-Factory, NeMo) and,
for some, clustering two Sparks over ConnectX-7.

**What hurts.** `apt upgrade` has repeatedly stranded the GPU driver
(driver 595 unsupported; kernels without `nvidia.ko`); `nvidia-smi`
reports memory as "Not Supported" on GB10, so nobody knows how much they
can load (the truthful number is `MemAvailable`); memory exhaustion wedges
the box instead of raising a clean OOM; the GDM greeter auto-suspends a
headless Spark after ~20 minutes and Wi-Fi profiles are per-user; the
273 GB/s bandwidth makes dense 70B models crawl (2.7 tok/s) while MoE
models fly; sm_121 wheels are missing from PyPI; EC firmware 0x03 on OEM
units causes thermal throttling; HDMI dies after deep sleep. Owners say,
in effect, "I want to develop *with* AI, not spend my time making the AI
work".

**Helpers that exist.** NVIDIA Sync (device discovery, tunnels, one-click
app scripts), the DGX Dashboard (updates, JupyterLab), NemoClaw/OpenShell
(a sandboxed always-on agent runtime), sparkrun (one-command LLM recipes
across nodes) and a cottage industry of unified-memory monitors. None is a
conversational on-device helper that knows the machine; the gap the owner
described is real.

**Implications for the roster (§3.2).** The device-settings specialist's
first job is *protective*: warn before `apt upgrade`, route updates
through the validated channel, and know the headless-suspend, docker-group
and UID-1000 pitfalls. The model sommelier must read `MemAvailable`, not
NVML, on unified memory and recommend MoE/NVFP4 first. The app installer's
catalogue is NVIDIA's 46 official playbooks, of which the first tier is:
Ollama (native, not Docker), Open WebUI, VS Code remote, ComfyUI, vLLM,
llama.cpp, LM Studio, NIM, Unsloth, Tailscale, Connect Two Sparks,
NemoClaw. A memory-headroom pill and a thermal/throttle watch belong in
`nvh status` and the mascot's tips.

**Sudo reality.** The OOBE user has sudo, but a password is required by
default; passwordless sudo is a user opt-in with a documented trade-off.
The helper therefore probes with `sudo -n` only, treats "in the sudo
group" as "can elevate with a password", and hands the exact command to a
terminal card the user runs when a password is needed.

## 6. Open decisions for the owner

1. **Mascot likeness.** Placeholder ships now; a Jensen sprite needs
   sign-off from the person and NVIDIA brand. Until then the manifest swap
   is documented in [MASCOT.md](../MASCOT.md).
2. **Sudo default.** Proposed: privileged tools enabled when `can_sudo` is
   true, every apply behind a red card, kill switch `NVH_ALLOW_PRIVILEGED=0`.
3. **Non-goals amendment.** "Everything works as an unprivileged user"
   stays true; add "and can do more, with approval, when the owner has
   root".

Back to [ROADMAP](../ROADMAP.md)
