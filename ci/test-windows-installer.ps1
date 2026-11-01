param([string]$InstallerPath = (Join-Path $PSScriptRoot '../install.ps1'))
$ErrorActionPreference = 'Stop'

# Parse actual installer control flow, never execute its network/install sections.
# Only user-environment calls and native commands in the selected fast path are
# replaced. No registry, filesystem installation, package, GPU, or model action.
class InstallerFakeEnvironment {
    static [hashtable] $Values = @{}
    static [System.Collections.Generic.List[string]] $Writes = [System.Collections.Generic.List[string]]::new()
    static [string] GetEnvironmentVariable([string]$Name, [string]$Scope) {
        if ($Scope -ne 'User') { throw 'Unexpected environment scope' }
        return [InstallerFakeEnvironment]::Values[$Name]
    }
    static [void] SetEnvironmentVariable([string]$Name, [string]$Value, [string]$Scope) {
        if ($Scope -ne 'User') { throw 'Unexpected environment scope' }
        [InstallerFakeEnvironment]::Values[$Name] = $Value
        [InstallerFakeEnvironment]::Writes.Add($Name)
    }
}
class InstallerTestExit : System.Exception {
    [int]$Code
    InstallerTestExit([int]$Code) { $this.Code = $Code }
}
function Assert-Equal($Expected, $Actual, [string]$Reason) {
    if ($Expected -cne $Actual) { throw "$Reason (expected '$Expected', actual '$Actual')" }
}
function Assert-Null($Actual, [string]$Reason) {
    if ($null -ne $Actual) { throw "$Reason (expected null, actual '$Actual')" }
}
function Assert-Contains([string]$Haystack, [string]$Needle, [string]$Reason) {
    if ($Haystack.IndexOf($Needle, [System.StringComparison]::Ordinal) -lt 0) { throw "$Reason (missing '$Needle')" }
}
function Assert-NotContains([string]$Haystack, [string]$Needle, [string]$Reason) {
    if ($Haystack.IndexOf($Needle, [System.StringComparison]::Ordinal) -ge 0) { throw "$Reason (found '$Needle')" }
}
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path -LiteralPath $InstallerPath).Path, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$statements = @($ast.EndBlock.Statements)
$firstFunction = @($statements | Where-Object {
    $_ -is [System.Management.Automation.Language.FunctionDefinitionAst]
})[0]
$resolution = @($statements | Where-Object {
    $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $_.Extent.StartOffset -lt $firstFunction.Extent.StartOffset
})
$helper = $ast.Find({ param($n)
    $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -eq 'Save-WorkspaceEnvironment'
}, $true)
if (-not $helper) { throw 'Installer must share successful-path workspace persistence.' }
$helperText = $helper.Extent.Text.Replace('[System.Environment]', '[InstallerFakeEnvironment]')
$fast = @($statements | Where-Object {
    $_ -is [System.Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -eq '(Test-Path $NVH_REPO) -and (Test-Path $NVH_VENV)'
})
if ($fast.Count -ne 1) { throw 'Expected one existing-install path' }
$fast = $fast[0]
$calls = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.CommandAst] -and
    $n.GetCommandName() -eq 'Save-WorkspaceEnvironment'
}, $true))
Assert-Equal 2 $calls.Count 'Both install paths must persist after success'
if ($calls[0].Extent.StartOffset -lt $fast.Extent.StartOffset -or
    $calls[0].Extent.EndOffset -gt $fast.Extent.EndOffset -or
    $calls[1].Extent.StartOffset -lt $fast.Extent.EndOffset) {
    throw 'Workspace persistence must remain in the existing success path and final fresh path'
}
$freshFailure = @($statements | Where-Object {
    $_ -is [System.Management.Automation.Language.IfStatementAst] -and
    $_.Extent.StartOffset -gt $fast.Extent.EndOffset -and
    $_.Clauses[0].Item1.Extent.Text -eq '$LASTEXITCODE -ne 0'
})
if ($freshFailure.Count -ne 1 -or $calls[1].Extent.StartOffset -lt $freshFailure[0].Extent.EndOffset) {
    throw 'Fresh package failure must precede persistence'
}
function Convert-TestFragment($Node) {
    $text = $Node.Extent.Text
    $replace = @($Node.FindAll({ param($n)
        ($n -is [System.Management.Automation.Language.CommandAst] -and
         $n.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Ampersand) -or
        $n -is [System.Management.Automation.Language.ExitStatementAst]
    }, $true))
    foreach ($n in ($replace | Sort-Object { $_.Extent.StartOffset } -Descending)) {
        if ($n -is [System.Management.Automation.Language.ExitStatementAst]) {
            $value = 'throw [InstallerTestExit]::new(' + $n.Pipeline.Extent.Text + ')'
        } else { $value = 'Invoke-TestNative' }
        $offset = $n.Extent.StartOffset - $Node.Extent.StartOffset
        $text = $text.Remove($offset, $n.Extent.Text.Length).Insert($offset, $value)
    }
    return $text
}
# One named function's definition text, so a pure helper can be dot-sourced on its own.
function Get-InstallerFunction([string]$Name) {
    $fn = $ast.Find({ param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name
    }, $true)
    if (-not $fn) { throw "Installer must define $Name" }
    return $fn
}
$fastText = Convert-TestFragment $fast
$freshFailureText = Convert-TestFragment $freshFailure[0]
$initialText = ($resolution | ForEach-Object { $_.Extent.Text }) -join "`n"
$savedProcess = @{}
$savedExitVariable = Get-Variable -Name LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue
$hadGlobalExit = $null -ne $savedExitVariable
$savedGlobalExit = if ($hadGlobalExit) { $savedExitVariable.Value } else { $null }
foreach ($name in @('NVH_HOME','NVHIVE_HOME','NVH_CONFIG','HIVE_CONFIG_HOME')) {
    $savedProcess[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$caseCount = 0
try {
    foreach ($route in @('fresh', 'existing', 'recreate')) {
        foreach ($case in @(
            @{ Name='default'; Inputs=@{}; Home=$null; Config=$null; Writes=0 },
            @{ Name='home'; Inputs=@{NVH_HOME='D:\chosen'}; Home='D:\chosen'; Config=$null; Writes=1 },
            @{ Name='alias'; Inputs=@{NVHIVE_HOME='D:\alias'}; Home='D:\alias'; Config=$null; Writes=1 },
            @{ Name='precedence'; Inputs=@{NVH_HOME='D:\chosen'; NVHIVE_HOME='D:\ignored'; NVH_CONFIG='D:\config'; HIVE_CONFIG_HOME='D:\ignored-config'}; Home='D:\chosen'; Config='D:\config'; Writes=3 },
            @{ Name='legacy-config'; Inputs=@{HIVE_CONFIG_HOME='D:\legacy'}; Home=$null; Config='D:\legacy'; Writes=2 },
            @{ Name='explicit-default'; Inputs=@{NVH_HOME='D:\chosen'; NVH_CONFIG='D:\chosen\config'}; Home='D:\chosen'; Config='D:\chosen\config'; Writes=3 }
        )) {
            & {
                foreach ($name in $savedProcess.Keys) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
                foreach ($name in $case.Inputs.Keys) { [Environment]::SetEnvironmentVariable($name, $case.Inputs[$name], 'Process') }
                [InstallerFakeEnvironment]::Values = @{ NVH_CONFIG='D:\stale'; HIVE_CONFIG_HOME='D:\stale' }
                [InstallerFakeEnvironment]::Writes.Clear()
                function Write-Green {} function Write-Blue {} function Write-Yellow {} function Write-Red {}
                function Write-Host {}
                function Test-Path($Path) { return -not ($route -eq 'recreate' -and $Path -like '*python.exe') }
                function Remove-Item {}
                function Invoke-TestNative { $script:LASTEXITCODE = 0; $global:LASTEXITCODE = 0 }
                . ([scriptblock]::Create($initialText))
                . ([scriptblock]::Create($helperText))
                if ($route -eq 'fresh') { Save-WorkspaceEnvironment } else {
                    try { . ([scriptblock]::Create($fastText)); throw 'Fast path did not exit' }
                    catch [InstallerTestExit] { Assert-Equal 0 $_.Exception.Code 'Successful reinstall exit' }
                }
                Assert-Equal $case.Writes ([InstallerFakeEnvironment]::Writes.Count) "$route $($case.Name) write count"
                if ($case.Home) { Assert-Equal $case.Home ([InstallerFakeEnvironment]::Values['NVH_HOME']) 'Future-shell canonical home' }
                if ($case.Config) {
                    Assert-Equal $case.Config ([InstallerFakeEnvironment]::Values['NVH_CONFIG']) 'Future-shell canonical config'
                    Assert-Equal $case.Config ([InstallerFakeEnvironment]::Values['HIVE_CONFIG_HOME']) 'Future-shell config alias'
                }
                [InstallerFakeEnvironment]::Writes.Clear()
                Save-WorkspaceEnvironment
                Assert-Equal 0 ([InstallerFakeEnvironment]::Writes.Count) 'Already-saved values must not be rewritten'
            }
            $caseCount++
        }
    }
    foreach ($route in @('fresh', 'existing', 'recreate')) {
        & {
            [InstallerFakeEnvironment]::Values = @{}
            [InstallerFakeEnvironment]::Writes.Clear()
            $env:NVH_HOME='D:\must-not-save'; $env:NVH_CONFIG='D:\must-not-save-config'
            function Write-Green {} function Write-Blue {} function Write-Yellow {} function Write-Red {}
            function Write-Host {} function Remove-Item {}
            function Test-Path($Path) { return -not ($route -eq 'recreate' -and $Path -like '*python.exe') }
            function Invoke-TestNative { $script:LASTEXITCODE = 17; $global:LASTEXITCODE = 17 }
            . ([scriptblock]::Create($initialText))
            . ([scriptblock]::Create($helperText))
            try {
                if ($route -eq 'fresh') {
                    $LASTEXITCODE = 17
                    . ([scriptblock]::Create($freshFailureText))
                    Save-WorkspaceEnvironment
                } else { . ([scriptblock]::Create($fastText)) }
                throw 'Failed package install did not exit'
            } catch [InstallerTestExit] { Assert-Equal 1 $_.Exception.Code 'Failure exit' }
            Assert-Equal 0 ([InstallerFakeEnvironment]::Writes.Count) 'Failed install must not persist settings'
        }
        $caseCount++
    }

    # -----------------------------------------------------------------------
    # ARM64 (Windows on Arm) contract. Mocked environment only: the pure
    # helpers are dot-sourced from the parsed installer and fed each
    # architecture signal, the WMI fallback is a fake, the checksum round
    # trip uses one temp file, and the Ollama section is checked by AST so
    # the arm64 path can never reach winget or OllamaSetup.exe (the x64
    # installer, which would run under emulation).
    # -----------------------------------------------------------------------
    & {
        function Write-Green {} function Write-Blue {} function Write-Yellow {} function Write-Red {} function Write-Gray {}
        function Write-Host {}
        foreach ($name in @('Get-HostArchitecture', 'Select-PreferredPython', 'Select-EmulatedPython',
                            'Get-OllamaAssetName', 'Get-ExpectedSha256', 'Test-FileSha256')) {
            . ([scriptblock]::Create((Get-InstallerFunction $name).Extent.Text))
        }

        # Architecture: an environment variable that says ARM64 is final — WMI is not consulted.
        function Get-CimInstance { param($ClassName, $ErrorAction) throw 'WMI must not be consulted when the environment already says ARM64' }
        Assert-Equal 'arm64' (Get-HostArchitecture -Wow64 'ARM64' -Native 'AMD64') 'x64 PowerShell under emulation (PROCESSOR_ARCHITEW6432=ARM64)'
        Assert-Equal 'arm64' (Get-HostArchitecture -Wow64 '' -Native 'ARM64') 'native ARM64 PowerShell (PROCESSOR_ARCHITECTURE=ARM64)'
        Assert-Equal 'arm64' (Get-HostArchitecture -Wow64 'arm64 ' -Native '') 'case and whitespace do not change the answer'
        # Neither variable says so: Win32_OperatingSystem.OSArchitecture decides.
        function Get-CimInstance { param($ClassName, $ErrorAction) Assert-Equal 'Win32_OperatingSystem' $ClassName 'WMI class'; [pscustomobject]@{ OSArchitecture = 'ARM 64-bit Processor' } }
        Assert-Equal 'arm64' (Get-HostArchitecture -Wow64 '' -Native 'AMD64') 'OSArchitecture fallback names ARM'
        function Get-CimInstance { param($ClassName, $ErrorAction) [pscustomobject]@{ OSArchitecture = '64-bit' } }
        Assert-Equal 'x64' (Get-HostArchitecture -Wow64 '' -Native 'AMD64') 'a plain 64-bit OS is x64'
        function Get-CimInstance { param($ClassName, $ErrorAction) throw 'no WMI here' }
        Assert-Equal 'x64' (Get-HostArchitecture -Wow64 '' -Native 'AMD64') 'WMI failure falls back to x64 (today''s behaviour)'
        Assert-Equal 'x64' (Get-HostArchitecture -Wow64 '' -Native '') 'nothing known is x64'

        # Python: native arm64 preferred on ARM64, x64 under emulation only as the fallback, first-found elsewhere.
        $native = @{ Path = 'C:\arm\python.exe'; Version = 'Python 3.12.9'; Machine = 'ARM64' }
        $x64    = @{ Path = 'C:\x64\python.exe'; Version = 'Python 3.12.9'; Machine = 'AMD64' }
        $pick = Select-PreferredPython -Candidates @($x64, $native) -HostArch 'arm64'
        Assert-Equal 'C:\arm\python.exe' $pick.Path 'ARM64 host prefers the native arm64 build even when an x64 one is first on PATH'
        Assert-Equal $false $pick.Emulated 'a native pick is not emulated'
        $pick = Select-PreferredPython -Candidates @($x64) -HostArch 'arm64'
        Assert-Equal 'C:\x64\python.exe' $pick.Path 'ARM64 host without a native build falls back to x64'
        Assert-Equal $true $pick.Emulated 'the x64 fallback is marked emulated so the installer says so'
        $pick = Select-PreferredPython -Candidates @($x64, $native) -HostArch 'x64'
        Assert-Equal 'C:\x64\python.exe' $pick.Path 'x64 host keeps the first candidate (today''s order)'
        Assert-Equal $false $pick.Emulated 'x64 on x64 is never emulated'
        Assert-Null (Select-PreferredPython -Candidates @() -HostArch 'arm64') 'no candidates, no pick'
        Assert-Equal 'C:\x64\python.exe' (Select-EmulatedPython -Candidates @($native, $x64)) 'the emulation fallback is the first x64 build'
        Assert-Null (Select-EmulatedPython -Candidates @($native)) 'no x64 build, no fallback'

        # Ollama asset per architecture, and the release manifest's exact shape ("<hex>  ./<name>").
        Assert-Equal 'ollama-windows-arm64.zip' (Get-OllamaAssetName 'arm64') 'ARM64 fetches the standalone arm64 CLI zip'
        Assert-Equal 'OllamaSetup.exe' (Get-OllamaAssetName 'x64') 'x64 keeps the installer'
        $hexA = 'a' * 64; $hexB = 'B' * 64
        $manifest = "$hexA  ./OllamaSetup.exe`r`n$hexB  ./ollama-windows-arm64.zip`n$('c' * 64)  ./install.ps1`n"
        Assert-Equal ($hexB.ToLower()) (Get-ExpectedSha256 -ManifestText $manifest -AssetName 'ollama-windows-arm64.zip') 'the arm64 zip line is found and lower-cased'
        Assert-Equal $hexA (Get-ExpectedSha256 -ManifestText $manifest -AssetName 'OllamaSetup.exe') 'CRLF lines parse'
        Assert-Null (Get-ExpectedSha256 -ManifestText $manifest -AssetName 'ollama-windows-amd64.zip') 'an asset the manifest does not list has no expectation'
        Assert-Null (Get-ExpectedSha256 -ManifestText '' -AssetName 'ollama-windows-arm64.zip') 'an empty manifest has no expectation'
        Assert-Null (Get-ExpectedSha256 -ManifestText "not a manifest`nabc  ./ollama-windows-arm64.zip" -AssetName 'ollama-windows-arm64.zip') 'a short hash is not a hash'

        # Checksum round trip on one temp file: match, tamper, no expectation, no file.
        $probe = Join-Path ([System.IO.Path]::GetTempPath()) ("nvh-installer-contract-" + [guid]::NewGuid().ToString('n') + ".bin")
        try {
            [System.IO.File]::WriteAllBytes($probe, [byte[]](1..64))
            $good = (Get-FileHash -Algorithm SHA256 -LiteralPath $probe).Hash
            Assert-Equal $true (Test-FileSha256 -Path $probe -Expected $good) 'a matching hash verifies'
            Assert-Equal $true (Test-FileSha256 -Path $probe -Expected $good.ToLower()) 'hash case does not matter'
            [System.IO.File]::WriteAllBytes($probe, [byte[]](1..65))
            Assert-Equal $false (Test-FileSha256 -Path $probe -Expected $good) 'a tampered file fails'
            Assert-Equal $false (Test-FileSha256 -Path $probe -Expected $null) 'no expectation never verifies'
            Assert-Equal $false (Test-FileSha256 -Path $probe -Expected '') 'an empty expectation never verifies'
            Assert-Equal $false (Test-FileSha256 -Path "$probe.missing" -Expected $good) 'a missing file never verifies'
        } finally {
            Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        }

        # Structure: the arm64 install function verifies before extracting and never names the x64 installer.
        $arm64Install = (Get-InstallerFunction 'Install-OllamaArm64').Extent.Text
        foreach ($needle in @('sha256sum.txt', 'Get-ExpectedSha256', 'Test-FileSha256', 'Expand-Archive', 'return $false')) {
            Assert-Contains $arm64Install $needle 'Install-OllamaArm64 must verify the release checksum before extracting'
        }
        foreach ($needle in @('OllamaSetup.exe', 'winget', 'ollama.com/download')) {
            Assert-NotContains $arm64Install $needle 'Install-OllamaArm64 must not reach for the x64 installer'
        }
        $extractOffset = $arm64Install.IndexOf('Expand-Archive', [System.StringComparison]::Ordinal)
        $verifyOffset = $arm64Install.IndexOf('Test-FileSha256', [System.StringComparison]::Ordinal)
        if ($verifyOffset -lt 0 -or $extractOffset -lt $verifyOffset) { throw 'Install-OllamaArm64 must verify the checksum before Expand-Archive' }

        # Structure: inside `if ($GPU_NAME)` the architecture branch sends arm64 to
        # Install-OllamaArm64 with the unverified-GPU note, and only the else
        # branch (x64) keeps winget + OllamaSetup.exe.
        $gpuBlock = @($statements | Where-Object {
            $_ -is [System.Management.Automation.Language.IfStatementAst] -and
            $_.Clauses[0].Item1.Extent.Text -eq '$GPU_NAME'
        })
        Assert-Equal 1 $gpuBlock.Count 'one GPU-gated Ollama section'
        $archBranches = @($gpuBlock[0].FindAll({ param($n)
            $n -is [System.Management.Automation.Language.IfStatementAst] -and
            $n.Clauses[0].Item1.Extent.Text -eq '$HOST_ARCH -eq "arm64"'
        }, $true))
        if ($archBranches.Count -lt 1) { throw 'The Ollama install must branch on $HOST_ARCH -eq "arm64"' }
        $installBranch = $archBranches[0]
        $arm64Clause = $installBranch.Clauses[0].Item2.Extent.Text
        Assert-Contains $arm64Clause 'Install-OllamaArm64' 'arm64 installs through the verified zip path'
        Assert-Contains $arm64Clause 'GPU acceleration is unverified on Windows on Arm' 'arm64 says the GPU note out loud before installing'
        foreach ($needle in @('OllamaSetup.exe', 'winget', 'Start-Process')) {
            Assert-NotContains $arm64Clause $needle 'the arm64 branch must never run the x64 installer'
        }
        if (-not $installBranch.ElseClause) { throw 'The x64 Ollama path must remain as the else branch' }
        $x64Clause = $installBranch.ElseClause.Extent.Text
        Assert-Contains $x64Clause 'winget install Ollama.Ollama' 'x64 keeps winget first'
        Assert-Contains $x64Clause 'OllamaSetup.exe' 'x64 keeps the installer fallback'
        # Every mention of the x64 installer in code lives in that else branch or in the pure asset-name helper.
        $assetFn = Get-InstallerFunction 'Get-OllamaAssetName'
        $setupMentions = @($ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $n.Value -like '*OllamaSetup.exe*'
        }, $true))
        if ($setupMentions.Count -lt 1) { throw 'Expected the x64 installer name somewhere in code' }
        foreach ($mention in $setupMentions) {
            $inElse = $mention.Extent.StartOffset -ge $installBranch.ElseClause.Extent.StartOffset -and
                      $mention.Extent.EndOffset -le $installBranch.ElseClause.Extent.EndOffset
            $inHelper = $mention.Extent.StartOffset -ge $assetFn.Extent.StartOffset -and
                        $mention.Extent.EndOffset -le $assetFn.Extent.EndOffset
            if (-not ($inElse -or $inHelper)) { throw "OllamaSetup.exe is reachable outside the x64 branch at offset $($mention.Extent.StartOffset)" }
        }

        # Structure: when nvidia-smi is absent, ARM64 gets the honest message, x64 keeps today's line.
        $noGpu = @($statements | Where-Object {
            $_ -is [System.Management.Automation.Language.IfStatementAst] -and
            $_.Clauses[0].Item1.Extent.Text -eq '-not $GPU_NAME'
        })
        Assert-Equal 1 $noGpu.Count 'one no-GPU message block'
        $noGpuArch = @($noGpu[0].FindAll({ param($n)
            $n -is [System.Management.Automation.Language.IfStatementAst] -and
            $n.Clauses[0].Item1.Extent.Text -eq '$HOST_ARCH -eq "arm64"'
        }, $true))
        Assert-Equal 1 $noGpuArch.Count 'the no-GPU message branches on the architecture'
        $noGpuArm = $noGpuArch[0].Clauses[0].Item2.Extent.Text
        Assert-Contains $noGpuArm 'nvidia-smi not found' 'ARM64 names the missing probe'
        Assert-Contains $noGpuArm 'unverified' 'ARM64 says local models are unverified'
        Assert-Contains $noGpuArm 'nvh setup' 'ARM64 points at the cloud providers that do work'
        if (-not $noGpuArch[0].ElseClause) { throw 'x64 must keep its no-GPU message' }
        Assert-Contains $noGpuArch[0].ElseClause.Extent.Text 'No NVIDIA GPU detected' 'x64 keeps today''s message'

        # Structure: the Python pick is architecture-aware and runs before the fast path.
        $pickCalls = @($ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Select-PreferredPython'
        }, $true))
        if ($pickCalls.Count -lt 1 -or $pickCalls[0].Extent.StartOffset -gt $fast.Extent.StartOffset) {
            throw 'The architecture-aware Python pick must precede the existing-install path'
        }
    }
    $caseCount++

    # -----------------------------------------------------------------------
    # Auto-config contract. Mocked environment only: the config helpers are
    # dot-sourced from the parsed installer with their one native call (the
    # venv python) swapped for a fake the way the fast path's are, so the only
    # write is one temp file. The installer must write what nvh's own
    # generate_default_config() printed (UTF-8, no byte-order mark), rewrite
    # only the Ollama default_model line to the tier pick, switch the Ollama
    # stanza on only for a GPU install (which sets Ollama up, tier pick or
    # not), and write nothing (naming `nvh config init`) when the python
    # answer fails or is empty.
    # -----------------------------------------------------------------------
    & {
        $said = [System.Collections.Generic.List[string]]::new()
        function Write-Green  { param($msg) $said.Add("green: $msg") }
        function Write-Yellow { param($msg) $said.Add("yellow: $msg") }
        function Write-Blue {} function Write-Red {} function Write-Gray {} function Write-Host {}
        foreach ($name in @('Get-NvhDefaultConfig', 'Set-NvhConfigOllamaModel', 'Write-NvhDefaultConfig')) {
            . ([scriptblock]::Create((Convert-TestFragment (Get-InstallerFunction $name))))
        }

        # Structure: the template is nvh's own, read from the venv interpreter's
        # stdout; no here-string placeholder remains; the one write sits behind
        # the existing-file check.
        $generatorText = (Get-InstallerFunction 'Get-NvhDefaultConfig').Extent.Text
        Assert-Contains $generatorText 'from nvh.config.settings import generate_default_config' 'the config is nvh''s own template'
        Assert-Contains $generatorText 'sys.stdout.write(generate_default_config())' 'the template is captured from stdout'
        Assert-NotContains $ast.Extent.Text '__NVH_DEFAULT_OLLAMA_MODEL__' 'no hand-typed config placeholder remains'
        $configCalls = @($ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Write-NvhDefaultConfig'
        }, $true))
        Assert-Equal 1 $configCalls.Count 'the fresh path writes the config once'
        $configGuard = $configCalls[0].Parent
        while ($configGuard -and -not ($configGuard -is [System.Management.Automation.Language.IfStatementAst])) { $configGuard = $configGuard.Parent }
        if (-not $configGuard) { throw 'The config write must stay behind the existing-file check' }
        Assert-Equal '-not (Test-Path -LiteralPath $configFile)' $configGuard.Clauses[0].Item1.Extent.Text 'an existing config.yaml is never overwritten, its path taken literally'
        if ($configGuard.Extent.StartOffset -lt $fast.Extent.EndOffset) { throw 'The config write belongs to the fresh path' }
        Assert-Contains $configCalls[0].Extent.Text '-EnableOllama:([bool]$GPU_NAME)' 'the Ollama stanza is switched on only for the Ollama a GPU install sets up'

        # The plumbing the mocked runs below swap out (Invoke-TestNative replaces the
        # whole `& $Python ... 2>&1` call): stderr is merged into the stream, the
        # native exit code is read right after the call inside the try, Python's
        # stdout is forced to UTF-8 for the call and both encodings are restored
        # in the finally, and the file is written as UTF-8 without a BOM.
        $generatorFn = Get-InstallerFunction 'Get-NvhDefaultConfig'
        $generatorCall = @($generatorFn.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.CommandAst] -and
            $n.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Ampersand
        }, $true))
        Assert-Equal 1 $generatorCall.Count 'the generator is one native call'
        Assert-Equal 1 $generatorCall[0].Redirections.Count 'the generator call carries one redirection'
        Assert-Equal '2>&1' $generatorCall[0].Redirections[0].Extent.Text 'stderr is merged so a traceback reaches neither the console nor the file'
        $generatorTry = @($generatorFn.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.TryStatementAst] -and $null -ne $n.Finally -and
            $n.Body.Extent.StartOffset -le $generatorCall[0].Extent.StartOffset -and
            $n.Body.Extent.EndOffset -ge $generatorCall[0].Extent.EndOffset
        }, $true))
        Assert-Equal 1 $generatorTry.Count 'the generator call runs inside one try/finally'
        $exitRead = @($generatorTry[0].Body.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $n.Left.Extent.Text -eq '$exit' -and $n.Right.Extent.Text -eq '$LASTEXITCODE'
        }, $true))
        Assert-Equal 1 $exitRead.Count 'the native exit code ($LASTEXITCODE, not $?) is read inside the try'
        if ($exitRead[0].Extent.StartOffset -lt $generatorCall[0].Extent.EndOffset) { throw 'the exit code must be read right after the generator call' }
        Assert-Contains $generatorTry[0].Body.Extent.Text '$env:PYTHONIOENCODING = "utf-8"' 'Python writes UTF-8 for the call whatever the console code page'
        Assert-Contains $generatorTry[0].Finally.Extent.Text '$env:PYTHONIOENCODING = $savedIoEncoding' 'PYTHONIOENCODING is restored afterwards'
        Assert-Contains $generatorTry[0].Finally.Extent.Text '[Console]::OutputEncoding = $savedConsoleEncoding' 'the console encoding is restored afterwards'
        $writerText = (Get-InstallerFunction 'Write-NvhDefaultConfig').Extent.Text
        Assert-Contains $writerText '[System.IO.File]::WriteAllLines' 'the file is written through .NET with an explicit encoding'
        Assert-Contains $writerText 'New-Object System.Text.UTF8Encoding $false' 'the file is UTF-8 without a byte-order mark'
        Assert-NotContains $writerText 'Set-Content' 'Set-Content -Encoding UTF8 writes a BOM under Windows PowerShell 5.1'

        # A stand-in template in the shape generate_default_config() prints (made-up ids: no real tag here).
        $template = @(
            'version: "1"',
            '',
            'advisors:',
            '  ollama:',
            '    base_url: http://127.0.0.1:11434',
            '    default_model: ollama/template-default',
            '    type: ollama',
            '    enabled: false',
            '',
            '  llm7:',
            '    api_key: ${LLM7_API_KEY:-anonymous}',
            '    default_model: served-default',
            '    fallback_model: served-fallback',
            '    enabled: true',
            '',
            '  groq:',
            '    api_key: ${GROQ_API_KEY}',
            '    default_model: groq/served-default',
            '    enabled: false',
            '',
            'budget:',
            '  daily_limit_usd: 5'
        )
        $ollamaLine = 5
        $ollamaEnabledLine = 7
        $fakePython = 'C:\fake\venv\Scripts\python.exe'
        $probe = Join-Path ([System.IO.Path]::GetTempPath()) ("nvh-installer-config-" + [guid]::NewGuid().ToString('n') + ".yaml")
        try {
            # Tier pick resolved, GPU install: the whole Ollama default_model line becomes the pick and the
            # stanza's `enabled: false` (right after `type: ollama`) is switched on; nothing else moves.
            function Invoke-TestNative { $script:LASTEXITCODE = 0; $global:LASTEXITCODE = 0; $template }
            Write-NvhDefaultConfig -Python $fakePython -Path $probe -Model 'picked-tag:7b' -EnableOllama
            $written = @(Get-Content -LiteralPath $probe -Encoding UTF8)
            Assert-Equal $template.Count $written.Count 'every template line is written, no more'
            Assert-Equal '    default_model: "ollama/picked-tag:7b"' $written[$ollamaLine] 'the Ollama default_model line is rewritten whole to the tier pick'
            Assert-Equal '    enabled: true' $written[$ollamaEnabledLine] 'the Ollama stanza is switched on for the Ollama this install sets up'
            for ($i = 0; $i -lt $template.Count; $i++) {
                if ($i -ne $ollamaLine -and $i -ne $ollamaEnabledLine) { Assert-Equal $template[$i] $written[$i] "line $i (cloud stanzas and the rest) is untouched" }
            }
            Assert-Equal 1 @($said | Where-Object { $_ -eq "green: Config created: $probe" }).Count 'the success message names the file'
            Assert-Equal 0 @($said | Where-Object { $_ -like 'yellow: *' }).Count 'no warning on success'
            $bytes = [System.IO.File]::ReadAllBytes($probe)
            if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
                throw 'config.yaml must not start with a byte-order mark: nvh''s loader opens it with the locale codec'
            }
            Assert-Equal 'version: "1"' ([System.Text.Encoding]::UTF8.GetString($bytes, 0, 12)) 'the file starts with the template''s first line, byte for byte'

            # Tier pick resolved, no GPU (no Ollama set up): the model line is rewritten, the stanza stays as nvh renders it.
            Remove-Item -LiteralPath $probe -Force
            $said.Clear()
            Write-NvhDefaultConfig -Python $fakePython -Path $probe -Model 'picked-tag:7b'
            $written = @(Get-Content -LiteralPath $probe -Encoding UTF8)
            Assert-Equal '    default_model: "ollama/picked-tag:7b"' $written[$ollamaLine] 'the tier pick is written without a GPU too'
            for ($i = 0; $i -lt $template.Count; $i++) {
                if ($i -ne $ollamaLine) { Assert-Equal $template[$i] $written[$i] "line $i is untouched without -EnableOllama" }
            }

            # No tier pick, GPU install: the template's own default_model stays, as `nvh config init` leaves it,
            # but the stanza is still switched on: this install sets Ollama up below either way.
            Remove-Item -LiteralPath $probe -Force
            $said.Clear()
            Write-NvhDefaultConfig -Python $fakePython -Path $probe -Model '' -EnableOllama
            $written = @(Get-Content -LiteralPath $probe -Encoding UTF8)
            Assert-Equal $template[$ollamaLine] $written[$ollamaLine] 'without a tier pick the template''s own Ollama default stays'
            Assert-Equal '    enabled: true' $written[$ollamaEnabledLine] 'without a tier pick the stanza is still switched on for the Ollama this install sets up'
            for ($i = 0; $i -lt $template.Count; $i++) {
                if ($i -ne $ollamaEnabledLine) { Assert-Equal $template[$i] $written[$i] "line $i is untouched without a tier pick" }
            }

            # No tier pick, no GPU: the template is written verbatim.
            Remove-Item -LiteralPath $probe -Force
            $said.Clear()
            Write-NvhDefaultConfig -Python $fakePython -Path $probe -Model ''
            $written = @(Get-Content -LiteralPath $probe -Encoding UTF8)
            Assert-Equal ($template -join "`n") ($written -join "`n") 'without a tier pick or a GPU the template is written verbatim'

            # stderr noise with a clean exit (a warning on import) never lands in the file.
            Remove-Item -LiteralPath $probe -Force
            $said.Clear()
            function Invoke-TestNative {
                $script:LASTEXITCODE = 0; $global:LASTEXITCODE = 0
                [System.Management.Automation.ErrorRecord]::new([System.Exception]::new('UserWarning: noise on stderr'), 'NativeCommandError', [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
                $template
            }
            Write-NvhDefaultConfig -Python $fakePython -Path $probe -Model 'picked-tag:7b'
            $written = @(Get-Content -LiteralPath $probe -Encoding UTF8)
            Assert-Equal $template.Count $written.Count 'stderr lines never land in config.yaml'
            Assert-Equal '    default_model: "ollama/picked-tag:7b"' $written[$ollamaLine] 'the rewrite still applies with stderr noise'

            # A failing generator (non-zero exit, even with partial output) writes nothing and says how to get the file.
            Remove-Item -LiteralPath $probe -Force
            $said.Clear()
            function Invoke-TestNative {
                $script:LASTEXITCODE = 1; $global:LASTEXITCODE = 1
                'version: "1"'
                [System.Management.Automation.ErrorRecord]::new([System.Exception]::new('ModuleNotFoundError: No module named nvh'), 'NativeCommandError', [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
            }
            Write-NvhDefaultConfig -Python $fakePython -Path $probe -Model 'picked-tag:7b'
            if (Test-Path -LiteralPath $probe) { throw 'a failing generator must write nothing' }
            $yellow = @($said | Where-Object { $_ -like 'yellow: *' })
            Assert-Equal 1 $yellow.Count 'one honest line on failure'
            Assert-Contains $yellow[0] 'nvh config init' 'the honest line names nvh config init'
            Assert-Contains $yellow[0] $probe 'the honest line names the file it did not write'
            Assert-Equal 0 @($said | Where-Object { $_ -like 'green: *' }).Count 'no "Config created" on failure'

            # An empty or whitespace-only answer with a clean exit is a failure too.
            foreach ($answer in @({ }, { '' }, { ''; '   ' })) {
                $said.Clear()
                function Invoke-TestNative { $script:LASTEXITCODE = 0; $global:LASTEXITCODE = 0; & $answer }
                Write-NvhDefaultConfig -Python $fakePython -Path $probe -Model 'picked-tag:7b'
                if (Test-Path -LiteralPath $probe) { throw 'an empty generator answer must write nothing' }
                Assert-Equal 1 @($said | Where-Object { $_ -like 'yellow: *nvh config init*' }).Count 'an empty answer gets the honest line'
                Assert-Equal 0 @($said | Where-Object { $_ -like 'green: *' }).Count 'an empty answer is not a created config'
            }

            # The rewrite is whole-line and Ollama-only: quoting and past corruption go, other models stay.
            $rewritten = Set-NvhConfigOllamaModel -Model 'picked-tag:7b' -Lines @(
                '    default_model: "ollama/old"-trailer"', "`tdefault_model: 'ollama/old'", '    fallback_model: ollama/keep', '    default_model: groq/keep', 'default_model: ollama/top')
            Assert-Equal '    default_model: "ollama/picked-tag:7b"' $rewritten[0] 'a corrupted quoted line is replaced whole'
            Assert-Equal "`tdefault_model: `"ollama/picked-tag:7b`"" $rewritten[1] 'indentation is kept whatever it is'
            Assert-Equal '    fallback_model: ollama/keep' $rewritten[2] 'fallback_model lines are not default_model lines'
            Assert-Equal '    default_model: groq/keep' $rewritten[3] 'a cloud default_model is never touched'
            Assert-Equal 'default_model: "ollama/picked-tag:7b"' $rewritten[4] 'no indentation is fine too'
            $kept = Set-NvhConfigOllamaModel -Model '' -Lines $template[0..4]
            Assert-Equal 5 @($kept).Count 'an empty pick keeps every line'
            Assert-Equal ($template[0..4] -join "`n") ($kept -join "`n") 'an empty pick changes nothing'
            # The switch flips only the `enabled: false` right after `type: ollama`; other bespoke stanzas keep theirs.
            $flipped = Set-NvhConfigOllamaModel -Model 'picked-tag:7b' -EnableOllama -Lines @(
                '  ollama:', '    type: ollama', '    enabled: false', '  mock:', '    type: mock', '    enabled: false', '    default_model: ollama/x')
            Assert-Equal '    enabled: true' $flipped[2] 'only the enabled line right after type: ollama is switched on'
            Assert-Equal '    enabled: false' $flipped[5] 'another bespoke stanza keeps its enabled line'
            Assert-Equal '    default_model: "ollama/picked-tag:7b"' $flipped[6] 'the model rewrite still applies alongside the flip'
            $flippedOnly = Set-NvhConfigOllamaModel -Model '' -EnableOllama -Lines @('    default_model: ollama/template-default', '    type: ollama', '    enabled: false')
            Assert-Equal '    default_model: ollama/template-default' $flippedOnly[0] 'no tier pick, the template''s own model line stays'
            Assert-Equal '    enabled: true' $flippedOnly[2] 'no tier pick, the stanza is still switched on: Ollama is set up either way'
        } finally {
            Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        }
    }
    $caseCount++
} finally {
    foreach ($name in $savedProcess.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedProcess[$name], 'Process')
    }
    if ($hadGlobalExit) { $global:LASTEXITCODE = $savedGlobalExit }
    else { Remove-Variable -Name LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue }
}
Write-Host "PASS: $caseCount Windows workspace contract cases (including the ARM64 and auto-config mocked cases); no installer or user-environment writes."
