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
} finally {
    foreach ($name in $savedProcess.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedProcess[$name], 'Process')
    }
    if ($hadGlobalExit) { $global:LASTEXITCODE = $savedGlobalExit }
    else { Remove-Variable -Name LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue }
}
Write-Host "PASS: $caseCount Windows workspace contract cases (including the ARM64 mocked case); no installer or user-environment writes."
