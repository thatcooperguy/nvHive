# Model Manager — the in-app model browser

nvHive knows your rig. It detected the GPU during install, so it can tell
you — *before* you download — whether a model fits your VRAM and how much
disk it needs. The Model Manager turns that knowledge into a one-click
browser: the LM Studio / Jan experience, on the rented GPU desktop nvHive
provisioned rootlessly.

There are two front doors: the **`/models`** page in the web dashboard and
the **`nvh models`** CLI. Both read the same VRAM-fit report, so the
dashboard and the terminal always agree on what fits.

## The web dashboard

Open the dashboard and click **Models** in the sidebar (or go to
`/models`). The header shows the detected GPU — VRAM and free disk — and
the page has two sections:

- **Installed** — every local model with its on-disk size and a **Remove**
  button that reclaims the space.
- **Catalog** — the fit-ranked catalog for *your* GPU. Each row shows:
  - a **fits GPU** / **tight fit** badge against your detected VRAM,
  - the estimated download size,
  - a **recommended** marker for the best first installs,
  - an **Install** button that streams live download progress.

Installs run in the background over server-sent events, so the progress
bar keeps moving even if you navigate away and come back. Cancel mid-pull
with the **Cancel** button.

Models live in Ollama under `$NVH_HOME/models`, so they survive reconnects
to the same workspace.

## The CLI

```bash
# Installed models + a hint (fast, no catalog)
nvh models list

# The full fit-ranked catalog for the detected GPU
nvh models list --all

# Install a model (streams ollama pull progress; Ctrl+C cancels)
nvh models pull gemma3:4b

# Remove a model and reclaim its disk
nvh models rm gemma3:4b        # add -y to skip the confirmation
```

`nvh models pull` runs `ollama pull` against the rootless Ollama binary
nvHive installed — no root, no system Ollama required. Use the pull target
shown in parentheses in `nvh models list --all` (for example
`gemma3:4b`), which is exactly what you'd type after `ollama pull`.

## How fit is decided

The catalog and the badges come from the same report the setup wizard
uses (`GET /v1/setup/model-fit`). For each candidate it compares the
model's recommended VRAM against your detected GPU and its estimated
on-disk size against your free space, then ranks by a fit score so the
best first install floats to the top. A **tight fit** badge doesn't block
the download — smaller quantizations and CPU offload can still make a
model usable — it's a heads-up that the model is at or above your VRAM.

## Under the hood

The Model Manager is a thin surface over endpoints that already powered
the setup wizard:

| Endpoint | Purpose |
|---|---|
| `GET /v1/setup/model-fit` | Fit-ranked catalog for the detected GPU/disk |
| `GET /v1/ollama/models` | Installed models with on-disk sizes |
| `POST /v1/ollama/pull` | Download a model (SSE progress) |
| `DELETE /v1/ollama/models/{name}` | Remove a model |

All are auth-gated when the workspace has an API key set; the web client
attaches the key automatically, including on the streaming pull.

## Troubleshooting

- **"Ollama binary not found"** on `nvh models pull` — the rootless local
  AI runtime isn't installed yet. Run
  `nvh workstation --with-local-ai -y`, then retry.
- **Catalog is empty or VRAM shows "unknown"** — GPU detection didn't
  find a card. `nvh doctor` reports what was (and wasn't) detected.
- **A pull stalls at 0%** — check that the Ollama service is up
  (`nvh status`); the Model Manager waits on the same runtime the Wizard
  uses.
