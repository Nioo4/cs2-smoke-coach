$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$verifyPath = Join-Path $PSScriptRoot '../verify.ps1'
$coverageGatePath = Join-Path $PSScriptRoot 'backend_coverage_gate.py'
$coverageGateSource = Get-Content -Raw -LiteralPath $coverageGatePath
$parseTokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($verifyPath, [ref]$parseTokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw 'verify.ps1 does not parse' }
$guard = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-TestProjectLabels' }, $true)
if ($null -eq $guard) { throw 'verification safety guard is missing' }
. ([scriptblock]::Create($guard.Extent.Text))

foreach ($required in @(
        'backend_coverage_gate.py',
        "'sh', '-ec'",
        'COVERAGE_FILE=/data/tmp/.coverage',
        'BACKEND_COVERAGE_GATE'
    )) {
    if (-not $ast.Extent.Text.Contains($required) -and -not $coverageGateSource.Contains($required)) {
        throw "backend coverage gate contract is missing: $required"
    }
}

$python = Join-Path $PSScriptRoot '../../backend/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'backend Python runtime is required for coverage gate boundary tests'
}
$coverageBoundaryProbe = @'
import sys
sys.path.insert(0, sys.argv[1])
from backend_coverage_gate import gate_exit_code
assert gate_exit_code(84.99) != 0
assert gate_exit_code(85.00) == 0
assert gate_exit_code(85.01) == 0
'@
$probeEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($coverageBoundaryProbe))
& $python -c "import base64; exec(base64.b64decode('$probeEncoded'))" $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'coverage gate boundary contract failed' }

$projectName = 'smokeline-e2e'
$script:fixtureKind = ''
$script:fixtureOwner = $projectName
$script:listFails = $false
$script:inspectFails = $false
$script:inspectMode = 'valid'

function docker {
    $global:LASTEXITCODE = 0
    if ($args -contains 'inspect') {
        if ($script:inspectFails) {
            $global:LASTEXITCODE = 1
            return
        }
        if ($script:inspectMode -eq 'missing') {
            '{"other.label":"not-the-project"}'
            return
        }
        if ($script:inspectMode -eq 'malformed') {
            '{'
            return
        }
        ('{"com.docker.compose.project":"' + $script:fixtureOwner + '"}')
        return
    }
    if ($script:listFails) {
        $global:LASTEXITCODE = 1
        return
    }
    $kind = if ($args[0] -eq 'ps') { 'container' } else { $args[0] }
    'another-project-resource'
    if ($kind -eq $script:fixtureKind) { 'smokeline-e2e_fixture' }
}

function Assert-Rejected {
    param([scriptblock]$Action)
    $rejected = $false
    try { & $Action } catch { $rejected = $true }
    if (-not $rejected) { throw 'unsafe resource state was accepted' }
}

Assert-TestProjectLabels -RequireEmpty
foreach ($kind in @('container', 'volume', 'network')) {
    $script:fixtureKind = $kind
    $script:fixtureOwner = $projectName
    $script:inspectMode = 'valid'
    $script:inspectFails = $false
    Assert-Rejected { Assert-TestProjectLabels -RequireEmpty }
    Assert-TestProjectLabels
    $script:fixtureOwner = 'another-project'
    Assert-Rejected { Assert-TestProjectLabels }
}
$script:fixtureKind = 'container'
$script:fixtureOwner = $projectName
$script:inspectMode = 'missing'
Assert-Rejected { Assert-TestProjectLabels }
$script:inspectMode = 'malformed'
Assert-Rejected { Assert-TestProjectLabels }
$script:inspectMode = 'valid'
$script:inspectFails = $true
Assert-Rejected { Assert-TestProjectLabels }
$script:listFails = $true
Assert-Rejected { Assert-TestProjectLabels -RequireEmpty }
Write-Host 'PASS: verification resource guards (no Docker or filesystem mutations)'
