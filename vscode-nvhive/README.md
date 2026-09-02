# nvHive VS Code Extension

A thin client for a running nvHive API server. Every command sends text to
the server and shows the reply in an "nvHive Agent" panel; no model runs
inside VS Code.

## Commands

Open the Command Palette (`Ctrl+Shift+P`) and type **nvHive**:

| Command | What it sends |
|---|---|
| Ask nvHive | Your prompt to `POST /v1/query`; nvHive's router picks the provider and model |
| Code Review | `git diff --cached` from the first workspace folder to `/v1/query` |
| Generate Tests | The active file to `/v1/query`; the reply opens in a new editor beside it |
| Explain Code | The current selection to `/v1/query` |
| Ask Council | Your question to `POST /v1/council`; shows the synthesized answer |

The status bar item reports whether `GET /v1/health` succeeded when the
extension activated.

## Requirements

An nvHive API server reachable at `nvhive.apiUrl` (default
`http://localhost:8000`):

```bash
pip install nvhive
nvh serve
```

The extension sends no credentials, so the server must be in open/local
mode (no `HIVE_API_KEY` and no user accounts configured).

## Running from source

The extension is not published to the Marketplace.

1. Open the `vscode-nvhive` folder in VS Code
2. `npm install && npm run compile`
3. Press F5 to launch the Extension Development Host

## Settings

| Setting         | Default                 | Description                  |
|-----------------|-------------------------|------------------------------|
| `nvhive.apiUrl` | `http://localhost:8000` | URL of the nvHive API server |
