# =============================================================================
# NVHive — Windows PowerShell Installer
#
# Install (run as regular user — no admin needed):
#   iwr -useb https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.ps1 | iex
#
# What lives in ~/nvh/:
#   ~/nvh/repo/       — the NVHive source code
#   ~/nvh/venv/       — Python virtual environment
#   ~/.hive/          — Config, database, API keys
# =============================================================================

$ErrorActionPreference = "Stop"

$NVH_HOME  = if ($env:NVH_HOME) { $env:NVH_HOME } else { "$HOME\nvh" }
$NVH_VENV  = "$NVH_HOME\venv"
$NVH_REPO  = "$NVH_HOME\repo"
$HIVE_DIR  = "$HOME\.hive"

function Write-Green  { param($msg) Write-Host $msg -ForegroundColor Green  }
function Write-Yellow { param($msg) Write-Host $msg -ForegroundColor Yellow }
function Write-Blue   { param($msg) Write-Host $msg -ForegroundColor Cyan   }
function Write-Red    { param($msg) Write-Host $msg -ForegroundColor Red    }
function Write-Gray   { param($msg) Write-Host $msg -ForegroundColor DarkGray }

Write-Host ""
Write-Green "╔══════════════════════════════════════╗"
Write-Green "║   NVHive — Windows Quick Install     ║"
Write-Green "╚══════════════════════════════════════╝"
Write-Host ""

# ---------------------------------------------------------------------------
# Find Python 3.11+
# ---------------------------------------------------------------------------
function Find-Python {
    foreach ($py in @("python3.12", "python3.11", "python3.13", "python3", "python")) {
        $p = Get-Command $py -ErrorAction SilentlyContinue
        if ($p) {
            $ver = & $p.Source --version 2>&1
            if ($ver -match "3\.(1[1-9]|[2-9]\d)") { return $p.Source }
        }
    }
    # Check py launcher (Windows Python Launcher)
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $ver = & py --version 2>&1
        if ($ver -match "3\.(1[1-9]|[2-9]\d)") { return "py" }
    }
    return $null
}

$PYTHON = Find-Python
if (-not $PYTHON) {
    Write-Yellow "Python 3.11+ not found."
    Write-Blue   "Trying winget install..."
    try {
        winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "User") + ";" + $env:PATH
        $PYTHON = Find-Python
    } catch {
        Write-Red "Could not install Python automatically."
        Write-Yellow "Please install Python 3.12 from https://python.org/downloads"
        Write-Yellow "Make sure to check 'Add Python to PATH' during install."
        exit 1
    }
}
Write-Gray "Python: $(& $PYTHON --version 2>&1) [$PYTHON]"

# ---------------------------------------------------------------------------
# Detect NVIDIA GPU via nvidia-smi
# ---------------------------------------------------------------------------
$GPU_NAME = ""
$VRAM_GB  = 0

$nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($nvidiaSmi) {
    try {
        $gpuInfo  = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null
        if ($gpuInfo) {
            $parts    = $gpuInfo.Trim() -split ",\s*"
            $GPU_NAME = $parts[0].Trim()
            $VRAM_MB  = [int]($parts[1] -replace "[^0-9]","")
            $VRAM_GB  = [math]::Floor($VRAM_MB / 1024)
            Write-Green "GPU: $GPU_NAME (${VRAM_GB}GB VRAM)"
        }
    } catch { }
}
if (-not $GPU_NAME) { Write-Yellow "No NVIDIA GPU detected — CPU mode" }

# ---------------------------------------------------------------------------
# Fast path: already installed
# ---------------------------------------------------------------------------
if ((Test-Path $NVH_REPO) -and (Test-Path $NVH_VENV)) {
    $venvPy = "$NVH_VENV\Scripts\python.exe"
    if (Test-Path $venvPy) {
        Write-Blue "Existing install found — updating..."
        & $venvPy -m pip install -q --upgrade pip 2>$null
        & $venvPy -m pip install -q -e "$NVH_REPO[serve,nvidia]" 2>$null
    } else {
        Write-Yellow "Recreating venv..."
        Remove-Item -Recurse -Force $NVH_VENV -ErrorAction SilentlyContinue
        & $PYTHON -m venv $NVH_VENV
        & "$NVH_VENV\Scripts\pip" install -q --upgrade pip 2>$null
        & "$NVH_VENV\Scripts\pip" install -q -e "$NVH_REPO[serve,nvidia]" 2>$null
    }
    Write-Green "NVHive ready."
    Write-Host ""
    Write-Host "  Type " -NoNewline; Write-Green "nvh" -NoNewline; Write-Host " to start chatting"
    Write-Host ""
    exit 0
}

# ---------------------------------------------------------------------------
# Fresh install
# ---------------------------------------------------------------------------
Write-Blue "Fresh install — setting up ~/nvh/..."
New-Item -ItemType Directory -Force -Path $NVH_HOME | Out-Null

# Clone repo
Write-Blue "Downloading NVHive..."
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    git clone --depth 1 -q https://github.com/thatcooperguy/nvHive.git $NVH_REPO 2>$null
    if ($LASTEXITCODE -ne 0) {
        New-Item -ItemType Directory -Force -Path $NVH_REPO | Out-Null
        Invoke-WebRequest -Uri "https://github.com/thatcooperguy/nvHive/archive/refs/heads/main.zip" `
            -OutFile "$NVH_HOME\nvhive.zip"
        Expand-Archive -Path "$NVH_HOME\nvhive.zip" -DestinationPath "$NVH_HOME\nvhive-main" -Force
        Move-Item "$NVH_HOME\nvhive-main\nvHive-main\*" $NVH_REPO -Force
        Remove-Item "$NVH_HOME\nvhive.zip","$NVH_HOME\nvhive-main" -Recurse -Force
    }
} else {
    New-Item -ItemType Directory -Force -Path $NVH_REPO | Out-Null
    Write-Blue "Downloading via zip (git not found)..."
    Invoke-WebRequest -Uri "https://github.com/thatcooperguy/nvHive/archive/refs/heads/main.zip" `
        -OutFile "$NVH_HOME\nvhive.zip"
    Expand-Archive -Path "$NVH_HOME\nvhive.zip" -DestinationPath "$NVH_HOME\_extract" -Force
    Move-Item "$NVH_HOME\_extract\nvHive-main\*" $NVH_REPO -Force
    Remove-Item "$NVH_HOME\nvhive.zip","$NVH_HOME\_extract" -Recurse -Force
}

# Create venv
Write-Blue "Creating Python environment..."
& $PYTHON -m venv $NVH_VENV
& "$NVH_VENV\Scripts\pip" install -q --upgrade pip 2>$null

# Install
Write-Blue "Installing NVHive (~60s)..."
& "$NVH_VENV\Scripts\pip" install -q -e "$NVH_REPO[serve,nvidia]" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Red "Install failed. Check Python version (need 3.11+)."
    exit 1
}

# ---------------------------------------------------------------------------
# Resolve the local model from the installed package
# ---------------------------------------------------------------------------
# nvh.core.local_models is the one VRAM-tier -> Ollama-tag table every ladder
# reads. `nvh models tiers --pick chat` sizes THIS machine's budget (nvidia-smi
# VRAM, or a unified pool minus the table's OS reserve) and prints the chat
# tag, so this installer never types a model name. History: until 0.42 this
# file carried its own three-rung nemotron / llama3.1:8b / nemotron-mini ladder.
$venvPython   = "$NVH_VENV\Scripts\python.exe"
$DefaultModel = ""
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $picked = & $venvPython -m nvh.cli.main models tiers --pick chat 2>$null
    if ($LASTEXITCODE -eq 0 -and $picked) {
        $DefaultModel = (@($picked) | Select-Object -Last 1).ToString().Trim()
    }
} catch { $DefaultModel = "" }
$ErrorActionPreference = $prevEap
if ($DefaultModel) {
    Write-Gray "Local model for this machine: $DefaultModel (nvh models tiers --pick chat)"
} else {
    Write-Yellow "Could not read the local model table from the installed nvh; pick a model later with 'nvh models pull --recommended'."
}

# ---------------------------------------------------------------------------
# Auto-config
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $HIVE_DIR | Out-Null
$configFile = "$HIVE_DIR\config.yaml"
if (-not (Test-Path $configFile)) {
    Write-Blue "Creating auto-config..."
    $configText = @'
version: "1"

defaults:
  mode: ask
  output: text
  stream: true
  max_tokens: 4096
  temperature: 1.0
  show_metadata: true

advisors:
  ollama:
    base_url: http://localhost:11434
    default_model: ollama/__NVH_DEFAULT_OLLAMA_MODEL__
    type: ollama
    enabled: true

  llm7:
    default_model: gpt-oss
    type: llm7
    enabled: true

  groq:
    api_key: ${GROQ_API_KEY}
    default_model: groq/openai/gpt-oss-120b
    enabled: false

  openai:
    api_key: ${OPENAI_API_KEY}
    default_model: gpt-5.6-terra
    enabled: false

  anthropic:
    api_key: ${ANTHROPIC_API_KEY}
    default_model: claude-sonnet-5
    enabled: false

budget:
  daily_limit_usd: 10
  monthly_limit_usd: 50
  hard_stop: true

cache:
  enabled: true
  ttl_seconds: 86400
  max_size: 1000
'@
    if ($DefaultModel) {
        $configText = $configText.Replace("__NVH_DEFAULT_OLLAMA_MODEL__", $DefaultModel)
    } else {
        # No tier resolved: drop the line so nvh's own default applies.
        $configText = (($configText -split "`r?`n") | Where-Object { $_ -notmatch "__NVH_DEFAULT_OLLAMA_MODEL__" }) -join "`n"
    }
    $configText | Set-Content -Path $configFile -Encoding UTF8
    Write-Green "Config created: $configFile"
}

# ---------------------------------------------------------------------------
# Add venv Scripts dir to user PATH
# ---------------------------------------------------------------------------
$scriptsDir = "$NVH_VENV\Scripts"
$userPath   = [System.Environment]::GetEnvironmentVariable("PATH", "User")
if ($userPath -notlike "*$scriptsDir*") {
    [System.Environment]::SetEnvironmentVariable(
        "PATH", "$scriptsDir;$userPath", "User"
    )
    $env:PATH = "$scriptsDir;$env:PATH"
    Write-Green "Added $scriptsDir to user PATH"
}

# ---------------------------------------------------------------------------
# Install Ollama for Windows (only if NVIDIA GPU present)
# ---------------------------------------------------------------------------
if ($GPU_NAME) {
    $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollamaCmd) {
        Write-Blue "Installing Ollama..."
        try {
            winget install Ollama.Ollama --accept-package-agreements --accept-source-agreements 2>$null
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "User") + ";$env:PATH"
            $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
        } catch { }

        if (-not $ollamaCmd) {
            Write-Yellow "winget install failed, trying direct download..."
            $ollamaInstaller = "$NVH_HOME\OllamaSetup.exe"
            Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" `
                -OutFile $ollamaInstaller
            Start-Process -FilePath $ollamaInstaller -ArgumentList "/SILENT" -Wait
            Remove-Item $ollamaInstaller -ErrorAction SilentlyContinue
            $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
        }
    }

    if ($ollamaCmd) {
        # Start Ollama service
        $ollamaRunning = try { (Invoke-WebRequest "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $false }
        if (-not $ollamaRunning) {
            Write-Blue "Starting Ollama..."
            Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
            Start-Sleep -Seconds 3
        }

        # The model is the tier table's chat pick for this machine (resolved
        # above via `nvh models tiers --pick chat`); nothing is typed here,
        # so a retired registry tag can never come back.
        $model = $DefaultModel

        $ollamaRunning = try { (Invoke-WebRequest "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $false }
        if ($ollamaRunning -and $model) {
            $modelList = ollama list 2>$null
            if ($modelList -notlike "*$model*") {
                Write-Blue "Pulling $model in background (you can start using nvh now)..."
                Start-Process ollama -ArgumentList "pull $model" -WindowStyle Hidden
            } else {
                Write-Green "Model $model ready."
            }
        }
    }
} else {
    Write-Yellow "No NVIDIA GPU — skipping Ollama install. Use cloud providers: nvh setup"
}

# ---------------------------------------------------------------------------
# Optional: Create Start Menu shortcut
# ---------------------------------------------------------------------------
try {
    $startMenu   = [System.Environment]::GetFolderPath("Programs")
    $shortcut    = "$startMenu\NVHive.lnk"
    $shell       = New-Object -ComObject WScript.Shell
    $lnk         = $shell.CreateShortcut($shortcut)
    $lnk.TargetPath    = "cmd.exe"
    $lnk.Arguments     = "/k `"$scriptsDir\nvh.exe`""
    $lnk.WorkingDirectory = $HOME
    $lnk.Description   = "NVHive Multi-LLM Orchestration"
    $lnk.Save()
    Write-Green "Start Menu shortcut created: NVHive"
} catch {
    # Non-fatal — shortcut creation can fail in some environments
}

Write-Host ""
Write-Green "╔══════════════════════════════════════╗"
Write-Green "║       NVHive is ready!               ║"
Write-Green "╚══════════════════════════════════════╝"
Write-Host ""
Write-Host "  " -NoNewline; Write-Green "nvh" -NoNewline
Write-Host "            Start chatting"
Write-Host "  " -NoNewline; Write-Green "nvh setup" -NoNewline
Write-Host "      Add more free AI providers"
Write-Host "  " -NoNewline; Write-Green "nvh status" -NoNewline
Write-Host "     System overview"
Write-Host ""
Write-Gray "  Install dir: ~/nvh/"
Write-Gray "  Config: ~/.hive/config.yaml"
Write-Host ""
Write-Gray "(Restart your terminal for PATH changes to take effect)"
Write-Host ""
