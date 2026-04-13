# nvHive VS Code Extension

Integrates the nvHive multi-LLM AI agent directly into VS Code.

## Features

- **Run Agent Task** -- describe what you need, nvHive picks the right model and tools
- **Code Review** -- review staged git changes with AI feedback
- **Generate Tests** -- generate tests for the current file
- **Explain Code** -- highlight code and get a plain-language explanation
- **Ask Council** -- pose a question to the full advisor council

## Requirements

The nvHive API server must be running:

```bash
nvh serve          # default: http://localhost:8000
```

Install nvHive from PyPI if you haven't already:

```bash
pip install nvhive
```

## Installation

1. Clone this repo and open the `vscode-nvhive` folder
2. `npm install && npm run compile`
3. Press F5 to launch the Extension Development Host

## Configuration

| Setting            | Default                  | Description                        |
|--------------------|--------------------------|------------------------------------|
| `nvhive.apiUrl`    | `http://localhost:8000`  | URL of the nvHive API server       |
| `nvhive.autoStart` | `true`                   | Auto-start `nvh serve` on activate |

## Usage

Open the Command Palette (`Ctrl+Shift+P`) and type **nvHive** to see available commands.
