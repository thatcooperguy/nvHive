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
} finally {
    foreach ($name in $savedProcess.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedProcess[$name], 'Process')
    }
    if ($hadGlobalExit) { $global:LASTEXITCODE = $savedGlobalExit }
    else { Remove-Variable -Name LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue }
}
Write-Host "PASS: $caseCount Windows workspace contract cases; no installer or user-environment writes."
