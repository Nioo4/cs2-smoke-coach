[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Load this shell's bundled ACL cmdlets without relying on module autoload.
$securityModule = Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1'
Import-Module -Name $securityModule -ErrorAction Stop

$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$cacheRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot '.workcache'))
$providerDir = [IO.Path]::GetFullPath((Join-Path $cacheRoot 'local-acceptance'))
$providerFile = [IO.Path]::GetFullPath((Join-Path $providerDir 'provider.json'))
if (-not $providerFile.StartsWith($cacheRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Output must stay inside the project .workcache directory.'
}
foreach ($path in @($projectRoot, $cacheRoot, $providerDir, $providerFile)) {
    if (Test-Path -LiteralPath $path) {
        if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Output paths must not be links or junctions.'
        }
    }
}
if (Test-Path -LiteralPath $providerFile) {
    throw 'provider.json already exists; refusing to overwrite local credentials.'
}
& git -C $projectRoot check-ignore --quiet -- '.workcache/local-acceptance/provider.json'
if ($LASTEXITCODE -ne 0) { throw 'Output is not gitignored. No credentials were requested.' }
$tracked = & git -C $projectRoot ls-files -- '.workcache/local-acceptance/provider.json'
if ($LASTEXITCODE -ne 0 -or $tracked) { throw 'Output must be untracked. No credentials were requested.' }

# Reuse the private-directory ACL pattern from setup-admin-password.ps1.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (Test-Path -LiteralPath $providerDir)) {
    New-Item -ItemType Directory -Path $providerDir | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetOwner($identity.User)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $identity.User, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'
    )
    $acl.AddAccessRule($rule)
    Microsoft.PowerShell.Security\Set-Acl -LiteralPath $providerDir -AclObject $acl
}
$currentAcl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $providerDir
if (-not $currentAcl.AreAccessRulesProtected -or
    $currentAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $identity.User.Value) {
    throw 'Local credential directory must be private and owned by the current account.'
}
foreach ($access in $currentAcl.Access) {
    if ($access.AccessControlType -eq 'Allow' -and
        $access.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $identity.User.Value) {
        throw 'Local credential directory permits another account. No credentials were requested.'
    }
}

Write-Host 'Local acceptance settings only; this does not enable a runtime or call a provider.'
Write-Host 'Enter the bare Beijing MaaS hostname from your provider endpoint (no https:// or path).'
$apiHost = Read-Host 'DASHSCOPE_API_HOST'
# Mirrors backend/app/core/config.py::_valid_dashscope_host.
$label = '[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
if ($apiHost -notmatch ('\A' + $label + '(?:\.' + $label + ')*\.cn-beijing\.maas\.aliyuncs\.com\z')) {
    throw 'Invalid DASHSCOPE_API_HOST; use a bare <prefix>.cn-beijing.maas.aliyuncs.com hostname.'
}
Write-Host 'Defaults are code defaults only; account access and model availability are not verified.'
$textModel = Read-Host 'QWEN_TEXT_MODEL [qwen3.7-flash-2026-07-15]'
if ([string]::IsNullOrWhiteSpace($textModel)) { $textModel = 'qwen3.7-flash-2026-07-15' }
$asrModel = Read-Host 'QWEN_ASR_MODEL [qwen-audio-3.0-asr-flash]'
if ([string]::IsNullOrWhiteSpace($asrModel)) { $asrModel = 'qwen-audio-3.0-asr-flash' }
Write-Host 'Enter the key only at the hidden prompt. Do not paste it into a command or chat.'
$secureKey = Read-Host 'DASHSCOPE_API_KEY' -AsSecureString
$keyPointer = [IntPtr]::Zero
$plainKey = $null
$json = $null
$stream = $null
try {
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if ([string]::IsNullOrWhiteSpace($plainKey)) { throw 'Empty key.' }
    $json = [ordered]@{
        AI_PROVIDER = 'dashscope'
        DASHSCOPE_API_HOST = $apiHost
        DASHSCOPE_API_KEY = $plainKey
        QWEN_TEXT_MODEL = $textModel.Trim()
        QWEN_ASR_MODEL = $asrModel.Trim()
    } | ConvertTo-Json
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
    $stream = [IO.File]::Open($providerFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush()
} catch {
    throw 'Local provider setup failed; no credential values are included in this error.'
} finally {
    if ($null -ne $stream) { $stream.Dispose() }
    if ($keyPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer) }
    $secureKey.Dispose()
    $plainKey = $null
    $json = $null
    if (Get-Variable bytes -ErrorAction SilentlyContinue) { [Array]::Clear($bytes, 0, $bytes.Length) }
}
Write-Host 'Saved .workcache/local-acceptance/provider.json for local acceptance only.'
Write-Host 'No provider requests, deployment, environment-file reads, or container changes were performed.'
