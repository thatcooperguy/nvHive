# Getting Started

nvHive turns a Linux machine with an NVIDIA GPU — rented or your own — into a
working AI lab without root and without Docker. This guide covers the install,
the first five minutes, the hardware you need, and what to do when something
breaks. Command details live in [COMMANDS.md](COMMANDS.md).

## Install

### One line, no root (Linux)

```bash
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash
```

The installer:

1. Picks a persistent home for `NVH_HOME` (see below), creates a self-contained
   venv under `$NVH_HOME/venv` and clones the source to `$NVH_HOME/repo`.
2. Detects the GPU with `nvidia-smi` and picks a local chat model that fits
   the VRAM ([MODELS.md](MODELS.md)). `NVH_INSTALL_MODEL_DOWNLOAD=0` skips
   the download.
3. Installs a rootless Ollama binary under `$NVH_HOME/bin` and checks the
   ports it needs (11434, 8000, 3000) for conflicts.
4. Appends an `export NVH_HOME=...` block to your shell profile
   (`# >>> nvhive rootless env >>>`) and writes `$NVH_HOME/nvh-env.sh`.
5. Ends with `nvh services start --open`: Ollama, then the API, then the WebUI,
   then a real end-to-end Wizard check. The browser opens only when every
   gate is green. `NVH_INSTALL_LAUNCH=0` installs without launching.

Cloud desktops usually have an ephemeral OS disk and one block-backed volume
that survives reconnects. Everything nvHive owns — venv, models, config, chats,
logs — lives under `NVH_HOME`, so that directory must be on the persistent
volume. The installer auto-detects a likely mount (it prefers `$HOME` when
`$HOME` is a large non-root, non-network filesystem, and honours `NVH_MOUNT`);
to choose it yourself, export it first:

```bash
export NVH_HOME=/mnt/persist/nvhive
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash
```

Avoid read-only CIFS/SMB shares and anything under `/tmp`. 200 GB or more is
comfortable for local LLMs plus ComfyUI assets; the installer checks free
space before every large download.

### Desktop session (click-first)

On a Linux *desktop* session use `start-linux.sh` instead. It runs the same
installer, creates an **NVHive AI Studio** launcher and `~/.local/bin/nvh`, and
opens the WebUI setup wizard so nobody has to source an env file:

```bash
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/start-linux.sh | bash
```

`NVH_USE_BINARY=1` makes it fetch the single-file binary from the
[Releases page](https://github.com/thatcooperguy/nvHive/releases/latest)
instead of using Python — useful on images with no working `python3`.

### pip (Python 3.11+ already available)

```bash
pip install nvhive        # extras: [serve] [mcp] [vision] [rag] [nvidia] [all]
nvh setup                 # free-tier wizard, no credit card
nvh "Hello!"
```

`nvh webui` starts the API and the dashboard; `nvh studio --install
rootless-ollama -y` adds local models later. From a clone use `pip install -e
".[dev]"`. Without `NVH_HOME` set, state defaults to `~/.nvh` and config to
`~/.hive/config.yaml` — fine on a laptop, wrong on an ephemeral VM.

### Reinstall or reset

```bash
bash "$NVH_HOME/uninstall.sh" --purge -y      # remove everything under NVH_HOME
nvh update                                    # upgrade an existing install
nvh snapshot save ~/nvhive.tar.gz             # bundle vault, RAG, receipts, chats
nvh snapshot restore ~/nvhive.tar.gz          # ...on a brand-new machine
```

Snapshots deliberately exclude `config.yaml` (it may hold raw keys); run
`nvh setup` again after a restore.

## The first five minutes

```bash
nvh "What is the CAP theorem?"          # route to the best available advisor
nvh ask "Review this" -p groq -f main.py # pick a provider, include a file
nvh ask "Summarise this contract" --local   # Ollama only; nothing leaves the box
nvh ask "What does HTTP 429 mean?" --fast   # cheapest/fastest advisor, no frills
nvh convene "Redis or Postgres for sessions?" --cabinet engineering
nvh status                               # what is configured, healthy, and routed
```

Every answer prints advisor, model, tokens, cost and latency unless you pass
`--quiet` or `--raw`. `nvh` on its own opens the REPL (`/help` lists its
commands; `/convene` toggles council mode, `/tools` enables tool use).

### Providers and keys

`nvh setup` enables the zero-signup providers (a local Ollama if one is
running, LLM7 anonymously) and walks through the free tiers that need a key.
`nvh keys` prints every signup link in one place. Keys can also arrive via
environment variables (`GROQ_API_KEY`, `OPENAI_API_KEY`, ...) or the **AI
Connections** page of the dashboard; `nvh advisor test` verifies them. The
full table is in [PROVIDERS.md](PROVIDERS.md).

### Council, cabinets, throwdown

`nvh convene` sends one question to several models in parallel and
synthesises a weighted answer. `--cabinet <name>` picks a preset panel of
personas (`nvh agent presets` lists them; `nvh agent analyze "question"`
previews the auto-generated panel). Students get teaching cabinets:

```bash
nvh convene "Explain recursion step by step" --cabinet code_tutor
nvh convene "Help me prepare for my calculus final" --cabinet exam_prep
```

`nvh throwdown` runs two passes — a panel answers, a second panel critiques,
then a final synthesis. Cabinets and their members are listed in
[CONFIGURATION.md](CONFIGURATION.md#council-cabinets).

### Budget

```bash
nvh config set budget.daily_limit_usd 5.00
nvh budget status
nvh savings
```

`budget.hard_stop: true` (the default) blocks queries once a limit is hit;
set it to `false` to warn instead.

## Hardware

| GPU | VRAM | What runs locally |
|---|---|---|
| none | — | cloud free tiers only (LLM7, Groq, Gemini, ...) |
| GTX 1660 / RTX 2060 / RTX 4060 | 6–8 GB | `gemma3:4b` + `moondream` vision |
| RTX 3060 / 4070 | 12 GB | `qwen3:8b` or `llama3.1:8b` + `minicpm-v` |
| RTX 4070 Ti / 4080 | 16 GB | `qwen2.5-coder:7b` + `llama3.2-vision` |
| RTX 3090 / 4090 / 5090 | 24–32 GB | `llama3.2-vision`, `nemotron-3-nano-omni` |
| A100 / H100 / RTX 6000 Pro | 40 GB+ | `nemotron-omni`, 70B-class coders |

CPU-only machines work: the installer skips the local model and `nvh setup`
configures the free cloud tiers. RAM 8 GB minimum (16 GB recommended); disk
~2 GB for the smallest local model. Models unload after inactivity, so a
gaming session gets its VRAM back. [MODELS.md](MODELS.md) has the fit logic,
the capability matrix and `nvh bench`.

## Running without root

Everything the installer does works as an unprivileged user; nothing needs
`sudo`, `apt`, or `systemctl`.

- **API keys** — on a headless box the OS keyring is often absent or slow, so
  nvHive reads keys from the environment and from `$NVH_HOME/config/.env`
  (where the Wizard and `nvh setup` write them) and only consults the keyring
  when `NVH_USE_KEYRING=1`. `cp .env.example "$NVH_HOME/config/.env"` gives
  you a template.
- **Node.js** — `nvh webui` installs Node 22 under `NVH_HOME` when the system
  has none.
- **Ports** — defaults are 11434 (Ollama), 8000 (API), 3000 (WebUI); all
  unprivileged. Port 80 is skipped automatically. `nvh serve --host 0.0.0.0`
  exposes the API on the network — set `HIVE_API_KEY` first.
- **Browser** — `nvh webui` honours `NVH_BROWSER`, then a rootless Firefox
  under `$NVH_HOME/apps/firefox`, then system browsers. It installs that
  Firefox itself on Linux x86_64 unless `NVH_FIREFOX_AUTO_INSTALL=0`.
- **Persistent service** — a systemd *user* unit needs no root:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/nvhive.service <<EOF
[Unit]
Description=nvHive API
[Service]
Environment=NVH_HOME=$NVH_HOME
ExecStart=$NVH_HOME/venv/bin/nvh serve --port 8000
Restart=on-failure
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now nvhive
loginctl enable-linger "$USER"      # survive logout
```

## Studio packs and the workstation

`nvh studio` installs optional tool packs into user-owned directories under
`NVH_HOME`. `nvh studio --list` shows exact pack status and disk estimates.

| Bundle | Command | Installs |
|---|---|---|
| AI Starter | `nvh studio --install starter -y` | rootless Ollama, recommended local LLMs, agent lab, ComfyUI power nodes, game-dev lab |
| LLMs | `nvh studio --install llms -y` | Gemma 3, Qwen 3, Llama 3.1, Qwen coder, DeepSeek reasoning, embeddings |
| Agents | `nvh studio --install agents -y` | LangGraph, CrewAI, AutoGen, JupyterLab, OpenClaw |
| ComfyUI | `nvh studio --install comfy -y` | ComfyUI Manager, Impact Pack, ControlNet Aux, Video Helper Suite, GGUF, rgthree |
| Creative | `nvh studio --install creative -y` | Blender LTS portable, launcher, asset workspace |
| Games | `nvh studio --install game -y` | Pygame/Panda3D lab, asset helpers, Wine mod workspace |
| Music | `nvh studio --install music -y` | ACE-Step, Demucs, WhisperX, Audacity/LMMS AppImages |
| Runtime fallback | `nvh studio --install python-runtime-fallback -y` | micromamba under `NVH_HOME` for images with a broken `venv` |

NemoClaw is the one pack that checks for Docker (it is an OpenShell sandbox
stack); the Wizard marks it blocked unless Docker already works without sudo.

`nvh workstation` wraps the student desktop flow: GPU detection, the desktop
launcher, boot checks, and — with `--all -y` — local AI, ComfyUI, the starter
pack and the WebUI in one go. The WebUI's **Memory Vault** keeps Markdown
notes under `$NVH_HOME/vault` (Obsidian installs rootlessly beside it when the
image allows AppImages).

## Spark playbooks

NVIDIA publishes step-by-step install guides for the DGX Spark in
[dgx-spark-playbooks](https://github.com/NVIDIA/dgx-spark-playbooks): Ollama,
Open WebUI, ComfyUI, vLLM, llama.cpp, LM Studio, Tailscale, VS Code, the DGX
Dashboard, CLI coding agents, OpenClaw and NemoClaw. nvHive ships them as
runnable plans. A playbook's id is the upstream folder name, so every step
traces back to its README (`nvidia/<id>`), and the plan mirrors the README's
own skeleton: what you'll accomplish, prerequisites, numbered steps, verify,
rollback, time and risk.

Unlike [studio packs](#studio-packs-and-the-workstation), some playbook steps
need `sudo`, so a playbook never runs silently:

- **Plan first.** Every command is shown before anything runs — tagged
  `sudo`, `user` or `manual` — with the verify commands, the undo commands and
  the estimated time and disk. Undo is preview text: nvHive never executes it.
- **Approve once.** In the Wizard, `playbook_plan` shows the plan and
  `playbook_install` needs a click on the red card that lists the exact
  commands (an approval token bound to that call, single use, 15 minutes). On
  the CLI, `nvh playbook install <id>` prints the same plan and asks once.
- **Skip what is done, stop at the first failure.** Each step has a check;
  steps whose check passes are skipped, so running a playbook again is safe.
- **No password ever reaches nvHive.** The Wizard's run uses `sudo -n` only
  where passwordless sudo exists. When sudo needs a password it stops and hands
  you one command for your own terminal — `nvh playbook install <id>` — where
  `sudo` prompts you directly.
- **A receipt and an audit note.** Every run that touches the host writes an
  install receipt (that is what `nvh playbook list` reports as installed) and
  a note under the vault's `Decisions/` folder — complete, partial or failed,
  with exit codes.

```bash
nvh playbook list             # id, title, sudo steps, manual steps, time, installed
nvh playbook plan ollama      # every command tagged sudo / user / manual, verify, undo
nvh playbook install ollama   # prints the plan, asks once, runs; sudo prompts here
nvh playbook install ollama -y --home-dir /mnt/persist/nvhive   # no prompt; another NVH_HOME
```

Some steps stay yours: browser logins, API tokens (`HF_TOKEN` and
`NGC_API_KEY` are declared prerequisites — nvHive never prompts for or stores
them), interactive TUIs such as `ollama run` or the OpenClaw and NemoClaw
onboarding, cabling, and servers that run in the foreground. The plan lists
them as `manual` steps. Two further rules of the road:

- **Docker.** Playbooks that need Docker start by adding you to the `docker`
  group when you are not in it; the run then stops and asks you to log out and
  back in (never `newgrp`). Run the playbook again afterwards — the finished
  steps are skipped.
- **Pipe-to-shell.** An upstream `curl … | sh` one-liner is never piped. The
  script is downloaded to `$NVH_HOME/playbooks/<id>/` and run from there; the
  plan marks the step `pipe-to-shell: unpinned` and quotes the upstream
  command verbatim. Where the README publishes a sha256 (ComfyUI), the
  download is verified first. A vendor package the README neither pins nor
  checksums (the VS Code "latest stable" .deb, installed as root) carries the
  same flag, shown as `unpinned download`, so the two installs read alike.
- **Cancelling.** Stopping a running playbook from the Jobs panel cannot
  interrupt the host command already in flight (it may finish on its own);
  the receipt and the vault note still record what ran and what was running,
  so `nvh playbook list` and the repair plan stay honest.

The DGX Dashboard's *Update Now* path (OS upgrade, firmware, reboot) is never
automated. Where a rootless studio pack does the same job (Ollama, OpenClaw,
NemoClaw) the plan names it as the alternative. `NVH_ALLOW_PRIVILEGED=0`
switches off the Wizard's `playbook_install` together with the other
privileged tools ([CONFIGURATION.md](CONFIGURATION.md#environment-variables));
the CLI path is unaffected because `sudo` asks you, not nvHive. Guides not
shipped yet — SGLang, NIM, Nemotron, Unsloth and the two networking guides
(connect two Sparks, connect to your Spark) — are listed by `nvh playbook
list` with the reason.

## If something breaks

```bash
nvh services            # per-service table: Ollama / API / WebUI, with the fix
nvh services restart    # recycle API + WebUI (Ollama keeps its warm model)
nvh status --deep       # config, keys, advisors, Ollama, GPU, disk, environment
nvh status --smoke      # offline workspace smoke test
nvh status --report     # redacted JSON support bundle under $NVH_HOME/support/
nvh repair              # safe, idempotent repairs; never sudo, never deletes assets
```

Logs are under `$NVH_HOME/logs/` (`install.log`, `api-server.log`,
`nvhive.log`). The dashboard's **Debug Report** button produces the same
redacted bundle as `nvh status --report`.

| Symptom | Fix |
|---|---|
| "No advisors available" | `nvh setup`, then `nvh status --providers` |
| Ollama `connection refused` | `nvh services start` re-spawns it; `curl localhost:11434/api/tags` to confirm |
| "Ollama binary not found" | `nvh workstation --with-local-ai -y` |
| Dashboard shows "API offline" forever | stale `nvh serve` whose engine failed to start — `nvh services restart` |
| Budget limit reached | `nvh budget status`; raise `budget.daily_limit_usd` or set `budget.hard_stop false` |
| `Address already in use` | `nvh serve --port 8001` / `nvh webui --port 3001` |
| WebUI never built | `nvh webui --install`, or `nvh webui --clean` to rebuild |
| Slow rig times out during bring-up | `NVH_OLLAMA_BOOT_TIMEOUT=30 NVH_API_BOOT_TIMEOUT=45 nvh services start` |

### Windows and macOS

Linux x86_64 is the supported target and the only one CI tests. Windows and
macOS binaries are published on the Releases page and `pip install nvhive`
works on both, best-effort. Windows notes:

- `nvh` sets `PYTHONIOENCODING=utf-8` at startup; when calling
  `python -m nvh.cli.main` directly, set it yourself or box-drawing output
  will raise `UnicodeEncodeError` under `cp1252`.
- A crash *after* correct output (`0xC0000005`) is CPython's
  `ProactorEventLoop` teardown bug; the CLI patches it, embedded use should
  copy the patch at the top of `nvh/cli/main.py`.
- Port 80 needs an elevated terminal — use `nvh webui --port 3000`.
- `pip install -e .` cannot overwrite a running `nvh.exe`; close terminals
  that used it, or run `python -m nvh.cli.main`.

## Next

- [MODELS.md](MODELS.md) — Model Manager, VRAM fit, capability matrix, `nvh bench`
- [PROVIDERS.md](PROVIDERS.md) — every provider, key variable and free tier
- [CONFIGURATION.md](CONFIGURATION.md) — `config.yaml`, env vars, `NVH_HOME` layout, cabinets, tools
- [WEBUI.md](WEBUI.md) — the dashboard
- [INTEGRATIONS.md](INTEGRATIONS.md) — SDK, REST, OpenAI proxy, MCP, Claude Code, NemoClaw

Back to [README](../README.md)
