# =============================================================================
# NVHive — Windows PowerShell Installer
#
# Install (run as regular user — no admin needed):
#   iwr -useb https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.ps1 | iex
#
# Everything lives in NVH_HOME (default ~/.nvh), exactly like install.sh:
#   $NVH_HOME\repo\       — the NVHive source code
#   $NVH_HOME\venv\       — Python virtual environment
#   $NVH_HOME\config\     — config.yaml, API keys (.env)   ($NVH_CONFIG / HIVE_CONFIG_HOME)
#   $NVH_HOME\state\      — the SQLite state database
#   $NVH_HOME\ollama\     — Ollama's standalone CLI, on Windows on Arm only
# Set NVH_HOME before running to install somewhere else (a persistent mount);
# the installer then records it in your user environment so every later
# `nvh` finds the same home.
#
# Windows on Arm (ARM64, RTX Spark-class devices): nvHive is untested there and
# ships no arm64 binary. This installer detects the architecture, prefers a
# native arm64 Python (falling back to an x64 Python under emulation when a
# dependency has no win_arm64 wheel — tiktoken has none today), never installs
# the x64 Ollama under emulation, and says plainly that GPU acceleration is
# unverified on Windows on Arm. Nothing here claims the device works.
# =============================================================================

$ErrorActionPreference = "Stop"

$NVH_HOME_CONFIGURED = [bool]($env:NVH_HOME -or $env:NVHIVE_HOME)
$NVH_HOME   = if ($env:NVH_HOME) { $env:NVH_HOME } elseif ($env:NVHIVE_HOME) { $env:NVHIVE_HOME } else { "$HOME\.nvh" }
$NVH_VENV   = "$NVH_HOME\venv"
$NVH_REPO   = "$NVH_HOME\repo"
# Config dir: NVH_CONFIG is the primary override, HIVE_CONFIG_HOME the legacy alias.
# An override is remembered in the user environment below (like NVH_HOME), so
# the config.yaml / .env written here is the one every later shell reads.
$NVH_CONFIG_CONFIGURED = [bool]($env:NVH_CONFIG -or $env:HIVE_CONFIG_HOME)
$NVH_CONFIG = if ($env:NVH_CONFIG) { $env:NVH_CONFIG } elseif ($env:HIVE_CONFIG_HOME) { $env:HIVE_CONFIG_HOME } else { "$NVH_HOME\config" }
$env:NVH_HOME         = $NVH_HOME
$env:NVH_CONFIG       = $NVH_CONFIG
$env:HIVE_CONFIG_HOME = $NVH_CONFIG

function Write-Green  { param($msg) Write-Host $msg -ForegroundColor Green  }
function Write-Yellow { param($msg) Write-Host $msg -ForegroundColor Yellow }
function Write-Blue   { param($msg) Write-Host $msg -ForegroundColor Cyan   }
function Write-Red    { param($msg) Write-Host $msg -ForegroundColor Red    }
function Write-Gray   { param($msg) Write-Host $msg -ForegroundColor DarkGray }

# Save explicit overrides only after a successful package installation. The
# legacy home alias is normalized to NVH_HOME for later shells. An explicitly
# chosen default config path must also replace a stale saved custom path.
function Save-WorkspaceEnvironment {
    if ($NVH_HOME_CONFIGURED) {
        $savedHome = [System.Environment]::GetEnvironmentVariable("NVH_HOME", "User")
        if ($savedHome -ne $NVH_HOME) {
            [System.Environment]::SetEnvironmentVariable("NVH_HOME", $NVH_HOME, "User")
            Write-Green "Saved NVH_HOME=$NVH_HOME to your user environment"
        }
    }
    if ($NVH_CONFIG_CONFIGURED) {
        foreach ($configVar in @("NVH_CONFIG", "HIVE_CONFIG_HOME")) {
            $savedConfig = [System.Environment]::GetEnvironmentVariable($configVar, "User")
            if ($savedConfig -ne $NVH_CONFIG) {
                [System.Environment]::SetEnvironmentVariable($configVar, $NVH_CONFIG, "User")
            }
        }
        Write-Green "Saved NVH_CONFIG=$NVH_CONFIG (and the HIVE_CONFIG_HOME alias) to your user environment"
    }
}

Write-Host ""
Write-Green "╔══════════════════════════════════════╗"
Write-Green "║   NVHive — Windows Quick Install     ║"
Write-Green "╚══════════════════════════════════════╝"
Write-Host ""

# ---------------------------------------------------------------------------
# Host architecture: x64, or ARM64 (Windows on Arm)
# ---------------------------------------------------------------------------
# ARM64 shows up three ways. A native ARM64 PowerShell reports
# PROCESSOR_ARCHITECTURE=ARM64; an x64 PowerShell running under emulation
# reports PROCESSOR_ARCHITEW6432=ARM64 (the host's real ISA, which WOW64 sets
# for the emulated process — exactly the correction nvh.utils.hw_ids makes);
# and when neither variable says so, Win32_OperatingSystem.OSArchitecture reads
# "ARM 64-bit Processor". Anything else is treated as x64 (today's behaviour).
# The parameters exist so ci/test-windows-installer.ps1 can feed each signal.
function Get-HostArchitecture {
    param(
        [string]$Wow64  = $env:PROCESSOR_ARCHITEW6432,
        [string]$Native = $env:PROCESSOR_ARCHITECTURE
    )
    foreach ($value in @($Wow64, $Native)) {
        if ($value -and ("$value".Trim() -ieq "ARM64")) { return "arm64" }
    }
    try {
        $osArch = (Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).OSArchitecture
        if ($osArch -and ("$osArch" -match "ARM")) { return "arm64" }
    } catch { }
    return "x64"
}

$HOST_ARCH = Get-HostArchitecture
if ($HOST_ARCH -eq "arm64") {
    Write-Yellow "Windows on Arm (ARM64) detected."
    Write-Yellow "nvHive is untested on Windows on Arm: there is no arm64 nvHive binary, local models and GPU acceleration are unverified here, and this installer prefers native arm64 tools where they exist."
}

# ---------------------------------------------------------------------------
# Find Python 3.11+
# ---------------------------------------------------------------------------
# Every 3.11+ interpreter on PATH, with the ISA it was built for:
# `platform.machine()` prints ARM64 for a native arm64 build and AMD64 for an
# x64 build — including one running under emulation on an ARM64 host.
function Get-PythonCandidates {
    $found = @()
    foreach ($py in @("python3.12", "python3.11", "python3.13", "python3", "python")) {
        $p = Get-Command $py -ErrorAction SilentlyContinue
        if ($p) {
            $ver = & $p.Source --version 2>&1
            if ($ver -match "3\.(1[1-9]|[2-9]\d)") {
                $machine = ""
                try { $machine = "$(& $p.Source -c 'import platform; print(platform.machine())' 2>$null | Select-Object -Last 1)".Trim() } catch { $machine = "" }
                $found += @{ Path = $p.Source; Version = "$ver".Trim(); Machine = $machine }
            }
        }
    }
    # Check py launcher (Windows Python Launcher)
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $ver = & py --version 2>&1
        if ($ver -match "3\.(1[1-9]|[2-9]\d)") {
            $machine = ""
            try { $machine = "$(& py -c 'import platform; print(platform.machine())' 2>$null | Select-Object -Last 1)".Trim() } catch { $machine = "" }
            $found += @{ Path = "py"; Version = "$ver".Trim(); Machine = $machine }
        }
    }
    return ,$found
}

# Pure: which interpreter to use. On x64 the first candidate, as before. On
# ARM64 the first native arm64 build; without one, the first x64 build, marked
# Emulated so the messages below can say so. `$null` when there is none.
function Select-PreferredPython {
    param([array]$Candidates, [string]$HostArch)
    $list = @($Candidates | Where-Object { $_ })
    if ($list.Count -eq 0) { return $null }
    if ($HostArch -ne "arm64") { return @{ Path = $list[0].Path; Emulated = $false } }
    foreach ($candidate in $list) {
        if ("$($candidate.Machine)" -ieq "ARM64") { return @{ Path = $candidate.Path; Emulated = $false } }
    }
    return @{ Path = $list[0].Path; Emulated = $true }
}

# Pure: the first x64 build among the candidates (the emulation fallback when
# a native arm64 install cannot build a dependency), or `$null`.
function Select-EmulatedPython {
    param([array]$Candidates)
    foreach ($candidate in @($Candidates | Where-Object { $_ })) {
        if ("$($candidate.Machine)" -ieq "AMD64") { return $candidate.Path }
    }
    return $null
}

$PY_CANDIDATES = Get-PythonCandidates
$pythonPick = Select-PreferredPython -Candidates $PY_CANDIDATES -HostArch $HOST_ARCH
$PYTHON = if ($pythonPick) { $pythonPick.Path } else { $null }
$PYTHON_EMULATED = [bool]($pythonPick -and $pythonPick.Emulated)
if (-not $PYTHON) {
    Write-Yellow "Python 3.11+ not found."
    Write-Blue   "Trying winget install..."
    try {
        if ($HOST_ARCH -eq "arm64") {
            # python.org ships a native arm64 3.12 and winget's manifest carries it;
            # an older winget without --architecture falls through to the default.
            winget install Python.Python.3.12 --architecture arm64 --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) {
                Write-Yellow "winget could not pick the arm64 build; installing the default (x64, runs under emulation)."
                winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
            }
        } else {
            winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        }
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "User") + ";" + $env:PATH
        $PY_CANDIDATES = Get-PythonCandidates
        $pythonPick = Select-PreferredPython -Candidates $PY_CANDIDATES -HostArch $HOST_ARCH
        $PYTHON = if ($pythonPick) { $pythonPick.Path } else { $null }
        $PYTHON_EMULATED = [bool]($pythonPick -and $pythonPick.Emulated)
        if (-not $PYTHON) { throw "python still not on PATH" }
    } catch {
        Write-Red "Could not install Python automatically."
        Write-Yellow "Please install Python 3.12 from https://python.org/downloads"
        Write-Yellow "Make sure to check 'Add Python to PATH' during install."
        exit 1
    }
}
Write-Gray "Python: $(& $PYTHON --version 2>&1) [$PYTHON]"
if ($PYTHON_EMULATED) {
    Write-Yellow "This is an x64 Python running under emulation (no native arm64 build found). It is the path we expect to work today: tiktoken, which nvHive's providers need, publishes no win_arm64 wheel."
}

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
if (-not $GPU_NAME) {
    if ($HOST_ARCH -eq "arm64") {
        # Honest, not reassuring: without nvidia-smi this installer cannot tell
        # whether an NVIDIA GPU is present, and it has no second probe to try.
        Write-Yellow "nvidia-smi not found. On Windows on Arm nvHive cannot tell whether this machine has an NVIDIA GPU until NVIDIA's driver is installed, and local models are unverified here either way."
        Write-Yellow "Cloud providers work now (nvh setup). If this is an RTX Spark-class device, install NVIDIA's Windows on Arm driver and re-run this installer to add Ollama."
    } else {
        Write-Yellow "No NVIDIA GPU detected — CPU mode"
    }
}

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
    if ($LASTEXITCODE -ne 0) {
        Write-Red "Install failed. Your saved workspace settings were not changed."
        exit 1
    }
    Save-WorkspaceEnvironment
    Write-Green "NVHive ready."
    Write-Host ""
    Write-Host "  Type " -NoNewline; Write-Green "nvh" -NoNewline; Write-Host " to start chatting"
    Write-Host ""
    exit 0
}

# ---------------------------------------------------------------------------
# Fresh install
# ---------------------------------------------------------------------------
Write-Blue "Fresh install — setting up $NVH_HOME ..."
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
    # On ARM64 with a native arm64 Python the usual cause is a dependency with
    # no win_arm64 wheel (tiktoken) that pip then tries to build without Rust.
    # One retry with an x64 Python under emulation, when the machine has one.
    $retried = $false
    if ($HOST_ARCH -eq "arm64" -and -not $PYTHON_EMULATED) {
        $fallbackPython = Select-EmulatedPython -Candidates $PY_CANDIDATES
        if ($fallbackPython) {
            Write-Yellow "pip could not install nvHive with the native arm64 Python (a dependency has no win_arm64 wheel; tiktoken is one). Retrying with the x64 Python at $fallbackPython under emulation..."
            Remove-Item -Recurse -Force $NVH_VENV -ErrorAction SilentlyContinue
            & $fallbackPython -m venv $NVH_VENV
            & "$NVH_VENV\Scripts\pip" install -q --upgrade pip 2>$null
            & "$NVH_VENV\Scripts\pip" install -q -e "$NVH_REPO[serve,nvidia]" 2>$null
            $retried = ($LASTEXITCODE -eq 0)
            if ($retried) { $PYTHON = $fallbackPython; $PYTHON_EMULATED = $true }
        } else {
            Write-Yellow "pip could not install nvHive with the native arm64 Python and no x64 Python is installed. Install Python 3.12 (x64) from https://python.org/downloads — it runs under emulation — or install Rust so pip can build tiktoken, then re-run."
        }
    }
    if (-not $retried) {
        Write-Red "Install failed. Check Python version (need 3.11+)."
        exit 1
    }
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
New-Item -ItemType Directory -Force -Path $NVH_CONFIG | Out-Null
$configFile = "$NVH_CONFIG\config.yaml"
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
# Ollama for Windows (only if NVIDIA GPU present)
# ---------------------------------------------------------------------------
# x64: winget, then OllamaSetup.exe — unchanged. ARM64: Ollama's installer is
# x64 only, so the one build that does not run under emulation is the
# standalone CLI zip from the same GitHub release (ollama-windows-arm64.zip),
# verified against that release's sha256sum.txt before anything is extracted
# into $NVH_HOME\ollama. Set NVH_OLLAMA_VERSION (a release tag such as
# v0.40.13) to pin a release instead of following "latest".
$OLLAMA_RELEASE_BASE = if ($env:NVH_OLLAMA_VERSION) {
    "https://github.com/ollama/ollama/releases/download/$($env:NVH_OLLAMA_VERSION)"
} else {
    "https://github.com/ollama/ollama/releases/latest/download"
}

# Pure: the release asset for a host architecture.
function Get-OllamaAssetName {
    param([string]$HostArch)
    if ($HostArch -eq "arm64") { return "ollama-windows-arm64.zip" }
    return "OllamaSetup.exe"
}

# Pure: the SHA-256 that a GNU-style manifest ("<hex>  ./<name>", one per
# line, the form Ollama publishes as sha256sum.txt) lists for one asset, or
# `$null` when the asset is not in it.
function Get-ExpectedSha256 {
    param([string]$ManifestText, [string]$AssetName)
    foreach ($line in ("$ManifestText" -split "`r?`n")) {
        if ($line -match '^\s*([0-9a-fA-F]{64})\s+\*?(?:\./)?(\S+)\s*$') {
            if ($matches[2] -ieq $AssetName) { return $matches[1].ToLower() }
        }
    }
    return $null
}

# Does the file on disk hash to the expected SHA-256? `$false` on any doubt
# (no expectation, unreadable file), never `$true` by default.
function Test-FileSha256 {
    param([string]$Path, [string]$Expected)
    if (-not $Expected) { return $false }
    try { $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLower() } catch { return $false }
    return ($actual -eq "$Expected".ToLower())
}

# The ARM64 branch: download the arm64 zip and its release manifest, verify,
# extract into $Destination, put it on the user PATH. `$true` only when
# ollama.exe is in place; every failure is said out loud and leaves nothing.
function Install-OllamaArm64 {
    param([string]$Destination)
    $asset = Get-OllamaAssetName "arm64"
    $zip = Join-Path $env:TEMP $asset
    try {
        Write-Blue "Downloading $asset (Ollama's native arm64 CLI; the Windows installer is x64 only)..."
        Invoke-WebRequest -Uri "$OLLAMA_RELEASE_BASE/$asset" -OutFile $zip -UseBasicParsing
        $manifest = (Invoke-WebRequest -Uri "$OLLAMA_RELEASE_BASE/sha256sum.txt" -UseBasicParsing).Content
        $expected = Get-ExpectedSha256 -ManifestText "$manifest" -AssetName $asset
        if (-not (Test-FileSha256 -Path $zip -Expected $expected)) {
            Write-Red "$asset does not match the release's sha256sum.txt (or the manifest does not list it); not installing it."
            return $false
        }
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        Expand-Archive -Path $zip -DestinationPath $Destination -Force
    } catch {
        Write-Red "Could not download or extract $asset ($($_.Exception.Message))."
        return $false
    } finally {
        Remove-Item $zip -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path (Join-Path $Destination "ollama.exe"))) {
        Write-Red "ollama.exe was not in $asset; nothing was installed."
        return $false
    }
    $userPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -notlike "*$Destination*") {
        [System.Environment]::SetEnvironmentVariable("PATH", "$Destination;$userPath", "User")
    }
    $env:PATH = "$Destination;$env:PATH"
    Write-Green "Ollama (arm64) installed to $Destination"
    return $true
}

if ($GPU_NAME) {
    $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollamaCmd) {
        if ($HOST_ARCH -eq "arm64") {
            Write-Yellow "Windows on Arm: GPU acceleration is unverified on Windows on Arm — nvHive has not tested Ollama's arm64 build against an NVIDIA GPU here, and it may run on the CPU."
            if (Install-OllamaArm64 -Destination "$NVH_HOME\ollama") {
                $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
            } else {
                Write-Yellow "Ollama was not installed. Cloud providers work now: nvh setup"
            }
        } else {
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
    if ($HOST_ARCH -eq "arm64") {
        Write-Yellow "No NVIDIA GPU visible — skipping Ollama. On Windows on Arm an arm64 Ollama exists, but GPU acceleration is unverified there, so nothing is installed without a driver. Use cloud providers: nvh setup"
    } else {
        Write-Yellow "No NVIDIA GPU — skipping Ollama install. Use cloud providers: nvh setup"
    }
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

Save-WorkspaceEnvironment

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
Write-Gray "  Install dir (NVH_HOME): $NVH_HOME"
Write-Gray "  Config: $NVH_CONFIG\config.yaml"
if ($HOST_ARCH -eq "arm64") {
    Write-Gray "  Windows on Arm: untested; GPU acceleration unverified. Hardware facts welcome via the RTX Spark issue template."
}
Write-Host ""
Write-Gray "(Restart your terminal for PATH changes to take effect)"
Write-Host ""
