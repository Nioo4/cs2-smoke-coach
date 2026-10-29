param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^sha256:[a-f0-9]{64}$')]
    [string]$BackendImageId
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# This interactive helper is run by the user, never with a password argument.
# The private output lives beside the repository, not inside it.
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$credentialDir = [IO.Path]::GetFullPath((Join-Path $projectRoot '../.smokeline-private'))
$hashFile = Join-Path $credentialDir 'admin-hash.env'
if (Test-Path -LiteralPath $hashFile) {
    throw 'The administrator hash already exists. It will not be overwritten.'
}

Get-Command docker -ErrorAction Stop | Out-Null
& docker image inspect $BackendImageId --format '{{.Id}}' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'The reviewed backend image is not available in this Docker context. No password was requested.'
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (Test-Path -LiteralPath $credentialDir)) {
    New-Item -ItemType Directory -Path $credentialDir -ErrorAction Stop | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetOwner($identity.User)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $identity.User, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'
    )
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $credentialDir -AclObject $acl
}
$directory = Get-Item -LiteralPath $credentialDir -Force
if (-not $directory.PSIsContainer -or ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'The private credential path must be a real directory, not a link.'
}
$currentAcl = Get-Acl -LiteralPath $credentialDir
if ($currentAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $identity.User.Value) {
    throw 'The private directory is owned by another account. No password was requested.'
}
if (-not $currentAcl.AreAccessRulesProtected) {
    throw 'The private directory inherits access permissions; review it before entering a password.'
}
foreach ($access in $currentAcl.Access) {
    if ($access.AccessControlType -eq 'Allow' -and
        $access.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $identity.User.Value) {
        throw 'The private directory permits another account. No password was requested.'
    }
}

# Single-quoted Python literals also survive Windows PowerShell 5.1 native args.
$createHash = @'
import os
from pathlib import Path
from app.scripts.create_admin_hash import create_hash

path = Path('/credentials/admin-hash.env')
if path.exists() or path.is_symlink():
    raise SystemExit('The hash already exists; refusing to overwrite it.')
try:
    value = create_hash()
except (EOFError, KeyboardInterrupt):
    raise SystemExit('Cancelled; no hash was saved.')
except ValueError:
    raise SystemExit('Empty password or passwords did not match; no hash was saved.')
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='ascii', newline='\n') as output:
    output.write('ADMIN_PASSWORD_HASH_B64=' + value + '\n')
    output.flush()
    os.fsync(output.fileno())
print('Saved the administrator hash. The plaintext password was not saved.')
'@

Write-Host 'Choose a NEW password that you have never sent in chat.'
Write-Host 'Enter it twice at the hidden prompts. Do not send the password or hash to anyone.'
$dockerArgs = @(
    'run', '--rm', '-it', '--pull', 'never', '--network', 'none', '--read-only',
    '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m', '--cap-drop', 'ALL',
    '--security-opt', 'no-new-privileges:true', '--entrypoint', 'python',
    '--mount', ('type=bind,source=' + $credentialDir + ',target=/credentials'),
    $BackendImageId, '-c', $createHash
)
& docker @dockerArgs
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $hashFile -PathType Leaf)) {
    throw 'Password setup did not complete. Report only the error, never a password or hash.'
}
Write-Host 'Done. Reply in chat only: password setup completed.'
