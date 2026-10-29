[CmdletBinding()]
param(
    [ValidateSet('ps51', 'pwsh')]
    [string]$Runner = 'pwsh'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-Condition {
    param(
        [bool]$Condition,
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function ConvertTo-PsLiteral {
    param([string]$Value)

    return ([char]39 + $Value.Replace([string][char]39, ([char]39 + [char]39)) + [char]39)
}

function Protect-TestDirectory {
    param(
        [string]$Path,
        [System.Security.Principal.SecurityIdentifier]$Owner
    )

    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetOwner($Owner)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $Owner, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'
    )
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Add-ForeignAllowWithIcacls {
    param(
        [string]$Path,
        [string]$OwnerSid,
        [string]$ForeignSid
    )

    $oldErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & icacls.exe $Path '/inheritance:r' '/grant:r' ('*' + $OwnerSid + ':(OI)(CI)(F)') '/grant' ('*' + $ForeignSid + ':(OI)(CI)(RX)') 2>&1 | Out-Null
        return [int]$LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }
}

function Invoke-HelperSource {
    param(
        [string]$Source,
        [string]$CredentialPath,
        [string]$RepositoryPath,
        [string]$ImageId,
        [hashtable]$State
    )

    $projectLine = '$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ''..''))'
    $credentialLine = '$credentialDir = [IO.Path]::GetFullPath((Join-Path $projectRoot ''../.smokeline-private''))'
    Assert-Condition ($Source.Contains($projectLine)) 'The helper project path changed; review this regression before running it.'
    Assert-Condition ($Source.Contains($credentialLine)) 'The helper credential path changed; review this regression before running it.'
    $safeSource = $Source.Replace(
        $projectLine,
        ('$projectRoot = ' + (ConvertTo-PsLiteral $RepositoryPath))
    ).Replace(
        $credentialLine,
        ('$credentialDir = ' + (ConvertTo-PsLiteral $CredentialPath))
    )
    $helperScript = [scriptblock]::Create($safeSource)
    & {
        param(
            [scriptblock]$Script,
            [string]$ReviewedImageId,
            [hashtable]$State
        )

        function docker {
            $call = @($args | ForEach-Object { [string]$_ })
            [void]$State.Calls.Add($call)
            if ($call.Count -ge 2 -and $call[0] -eq 'image' -and $call[1] -eq 'inspect') {
                Set-Variable -Name LASTEXITCODE -Value 0 -Scope 1
                return
            }
            if ($call.Count -gt 0 -and $call[0] -eq 'run') {
                $State.RunCount = [int]$State.RunCount + 1
                [IO.File]::WriteAllText(
                    $State.HashPath,
                    'ADMIN_PASSWORD_HASH_B64=TEST_STUB_VALUE' + "`n",
                    [Text.Encoding]::ASCII
                )
                Set-Variable -Name LASTEXITCODE -Value 0 -Scope 1
                return
            }
            throw 'Unexpected Docker command in the isolated helper test.'
        }

        & $Script -BackendImageId $ReviewedImageId | Out-Null
    } $helperScript $ImageId $State
}

function Invoke-RealPython {
    param(
        [string]$ImageId,
        [string]$MountPath,
        [string]$PythonCode
    )

    $dockerArgs = @(
        'run', '--rm', '-it', '--pull', 'never', '--network', 'none', '--read-only',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges:true', '--entrypoint', 'python',
        '--mount', ('type=bind,source=' + $MountPath + ',target=/credentials'),
        $ImageId, '-c', $PythonCode
    )
    $oldErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & docker.exe @dockerArgs 2>&1 | Out-Null
        return [int]$LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$helperPath = Join-Path $repoRoot 'scripts/setup-admin-password.ps1'
$workcacheRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.workcache'))
$fixtureName = 'setup-admin-password-test-' + $Runner
$fixtureRoot = [IO.Path]::GetFullPath((Join-Path $workcacheRoot $fixtureName))
$markerPath = Join-Path $fixtureRoot '.fixture-created'
$markerValue = 'SmokeLine setup-admin-password regression fixture'
$fixtureCreated = $false

Assert-Condition (Test-Path -LiteralPath $workcacheRoot -PathType Container) 'The repository .workcache directory is required.'
Assert-Condition ((((Get-Item -LiteralPath $workcacheRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)) 'The repository .workcache path must not be a link.'
$resolvedCache = [IO.Path]::GetFullPath($workcacheRoot).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
$fixturePrefix = $resolvedCache + [IO.Path]::DirectorySeparatorChar
$resolvedFixture = [IO.Path]::GetFullPath($fixtureRoot)
Assert-Condition ($resolvedFixture.StartsWith($fixturePrefix, [StringComparison]::OrdinalIgnoreCase)) 'The test fixture is outside .workcache.'
Assert-Condition ((Split-Path -Leaf $resolvedFixture) -eq $fixtureName) 'The test fixture name is not fixed.'
Assert-Condition (-not (Test-Path -LiteralPath $resolvedFixture)) 'A previous fixture exists; refusing recursive cleanup.'

New-Item -ItemType Directory -Path $resolvedFixture -ErrorAction Stop | Out-Null
[IO.File]::WriteAllText($markerPath, $markerValue, [Text.Encoding]::ASCII)
$fixtureCreated = $true

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentSid = $identity.User
$helperSource = [IO.File]::ReadAllText($helperPath)
Assert-Condition (Test-Path -LiteralPath $helperPath -PathType Leaf) 'The password helper is missing.'
Assert-Condition (-not $helperSource.Contains('$credentialDir = ' + (ConvertTo-PsLiteral (Join-Path $repoRoot '../.smokeline-private')))) 'The regression cannot prove the private path is replaced.'

try {
    $imageProbe = & docker.exe image inspect smokeline-backend:phase1 --format '{{.Id}}' 2>$null
    Assert-Condition ($LASTEXITCODE -eq 0) 'The reviewed backend image is unavailable; the real Docker regression cannot run.'
    $imageId = ([string]$imageProbe).Trim()
    Assert-Condition ($imageId -match '^sha256:[a-f0-9]{64}$') 'Docker returned an invalid reviewed image ID.'

    $existingDir = Join-Path $resolvedFixture 'existing'
    New-Item -ItemType Directory -Path $existingDir | Out-Null
    $existingHash = Join-Path $existingDir 'admin-hash.env'
    $existingValue = 'ADMIN_PASSWORD_HASH_B64=HOST_SENTINEL' + "`n"
    [IO.File]::WriteAllText($existingHash, $existingValue, [Text.Encoding]::ASCII)
    $rejected = $false
    $rejectionMessage = ''
    $existingState = @{
        Calls = New-Object System.Collections.ArrayList
        RunCount = 0
        HashPath = $existingHash
    }
    try {
        Invoke-HelperSource $helperSource $existingDir $repoRoot $imageId $existingState
    }
    catch {
        $rejected = $true
        $rejectionMessage = $_.Exception.Message
    }
    Assert-Condition $rejected 'An existing administrator hash was not rejected.'
    Assert-Condition ($rejectionMessage -eq 'The administrator hash already exists. It will not be overwritten.') 'The existing-hash refusal text changed unexpectedly.'
    Assert-Condition (([IO.File]::ReadAllText($existingHash)) -eq $existingValue) 'The existing administrator hash was changed.'
    Assert-Condition ($existingState.Calls.Count -eq 0) 'Docker was called before the existing-hash refusal.'

    $inheritedDir = Join-Path $resolvedFixture 'inherited'
    New-Item -ItemType Directory -Path $inheritedDir | Out-Null
    $inheritedAcl = Get-Acl -LiteralPath $inheritedDir
    Assert-Condition (-not $inheritedAcl.AreAccessRulesProtected) 'The inherited ACL fixture is not inherited on this host.'
    $rejected = $false
    $rejectionMessage = ''
    $inheritedState = @{
        Calls = New-Object System.Collections.ArrayList
        RunCount = 0
        HashPath = Join-Path $inheritedDir 'admin-hash.env'
    }
    try {
        Invoke-HelperSource $helperSource $inheritedDir $repoRoot $imageId $inheritedState
    }
    catch {
        $rejected = $true
        $rejectionMessage = $_.Exception.Message
    }
    Assert-Condition $rejected 'An inherited private directory was not rejected.'
    Assert-Condition ($rejectionMessage -eq 'The private directory inherits access permissions; review it before entering a password.') 'The inherited-ACL refusal text changed unexpectedly.'
    Assert-Condition ($inheritedState.RunCount -eq 0) 'Docker run was called for an inherited private directory.'

    $foreignDir = Join-Path $resolvedFixture 'foreign-allow'
    New-Item -ItemType Directory -Path $foreignDir | Out-Null
    Protect-TestDirectory $foreignDir $currentSid
    $everyoneSid = New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
    $icaclsExit = Add-ForeignAllowWithIcacls $foreignDir $currentSid.Value $everyoneSid.Value
    Assert-Condition ($icaclsExit -eq 0) 'icacls could not create the foreign-Allow ACL fixture.'
    $foreignAcl = Get-Acl -LiteralPath $foreignDir
    Assert-Condition $foreignAcl.AreAccessRulesProtected 'The foreign-allow ACL fixture unexpectedly inherits.'
    Assert-Condition (@($foreignAcl.Access | Where-Object {
        $_.AccessControlType -eq 'Allow' -and
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $everyoneSid.Value
    }).Count -gt 0) 'The foreign-allow ACL fixture was not installed.'
    $rejected = $false
    $rejectionMessage = ''
    $foreignState = @{
        Calls = New-Object System.Collections.ArrayList
        RunCount = 0
        HashPath = Join-Path $foreignDir 'admin-hash.env'
    }
    try {
        Invoke-HelperSource $helperSource $foreignDir $repoRoot $imageId $foreignState
    }
    catch {
        $rejected = $true
        $rejectionMessage = $_.Exception.Message
    }
    Assert-Condition $rejected 'A private directory with a foreign Allow rule was not rejected.'
    Assert-Condition ($rejectionMessage -eq 'The private directory permits another account. No password was requested.') 'The foreign-Allow refusal text changed unexpectedly.'
    Assert-Condition ($foreignState.RunCount -eq 0) 'Docker run was called for a foreign Allow rule.'

    $positiveDir = Join-Path $resolvedFixture 'positive'
    $positiveState = @{
        Calls = New-Object System.Collections.ArrayList
        RunCount = 0
        HashPath = Join-Path $positiveDir 'admin-hash.env'
    }
    Invoke-HelperSource $helperSource $positiveDir $repoRoot $imageId $positiveState
    $positiveAcl = Get-Acl -LiteralPath $positiveDir
    Assert-Condition ($positiveAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq $currentSid.Value) 'The positive ACL owner is not the current SID.'
    Assert-Condition $positiveAcl.AreAccessRulesProtected 'The positive ACL still inherits permissions.'
    Assert-Condition (@($positiveAcl.Access | Where-Object {
        $_.AccessControlType -eq 'Allow' -and
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $currentSid.Value
    }).Count -eq 0) 'The positive ACL permits a foreign account.'
    $positiveHash = Join-Path $positiveDir 'admin-hash.env'
    Assert-Condition (Test-Path -LiteralPath $positiveHash -PathType Leaf) 'The helper success path did not create its test hash.'
    $positiveRun = @($positiveState.Calls | Where-Object { $_.Count -gt 0 -and $_[0] -eq 'run' })
    Assert-Condition ($positiveRun.Count -eq 1) 'The helper did not issue exactly one Docker run.'
    $positiveArgs = $positiveRun[0]
    foreach ($requiredArg in @('-it', '--pull', 'never', '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--entrypoint', 'python', '-c')) {
        Assert-Condition ($positiveArgs -contains $requiredArg) ('The Docker argument ' + $requiredArg + ' is missing.')
    }
    $mountArgs = @($positiveArgs | Where-Object { $_ -like 'type=bind,source=*,target=/credentials' })
    Assert-Condition ($mountArgs.Count -eq 1) 'The credentials bind mount argument is missing or ambiguous.'
    Assert-Condition ($mountArgs[0] -notmatch '\.smokeline-private') 'The isolated helper test attempted to mount the real private directory.'

    $realDir = Join-Path $resolvedFixture 'real-docker'
    New-Item -ItemType Directory -Path $realDir | Out-Null
    Protect-TestDirectory $realDir $currentSid
    $realAcl = Get-Acl -LiteralPath $realDir
    Assert-Condition ($realAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq $currentSid.Value) 'The real Docker fixture owner is not the current SID.'
    Assert-Condition $realAcl.AreAccessRulesProtected 'The real Docker fixture inherits permissions.'
    Assert-Condition (@($realAcl.Access | Where-Object {
        $_.AccessControlType -eq 'Allow' -and
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $currentSid.Value
    }).Count -eq 0) 'The real Docker fixture permits a foreign account.'

    $pythonMatch = [regex]::Match($helperSource, '(?s)\$createHash = @''(?<code>.*?)''@')
    Assert-Condition $pythonMatch.Success 'The embedded Python block is missing.'
    $pythonCode = $pythonMatch.Groups['code'].Value
    Assert-Condition ($pythonCode.Contains('value = create_hash()')) 'The expected password-call seam is missing from embedded Python.'
    $pythonCode = $pythonCode.Replace('value = create_hash()', "value = 'REGRESSION_TEST_VALUE'")
    $realHash = Join-Path $realDir 'admin-hash.env'
    $firstExit = Invoke-RealPython $imageId ([IO.Path]::GetFullPath($realDir)) $pythonCode
    Assert-Condition ($firstExit -eq 0) 'The real Docker first-write regression failed.'
    Assert-Condition (Test-Path -LiteralPath $realHash -PathType Leaf) 'The real Docker first-write regression produced no hash file.'
    $realValue = [IO.File]::ReadAllText($realHash)
    $secondExit = Invoke-RealPython $imageId ([IO.Path]::GetFullPath($realDir)) $pythonCode
    Assert-Condition ($secondExit -ne 0) 'The ordinary second helper run unexpectedly succeeded.'
    Assert-Condition (([IO.File]::ReadAllText($realHash)) -eq $realValue) 'The ordinary second helper run changed the existing hash.'

    $raceDir = Join-Path $resolvedFixture 'race'
    New-Item -ItemType Directory -Path $raceDir | Out-Null
    Protect-TestDirectory $raceDir $currentSid
    $raceAcl = Get-Acl -LiteralPath $raceDir
    Assert-Condition ($raceAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq $currentSid.Value) 'The race fixture owner is not the current SID.'
    Assert-Condition $raceAcl.AreAccessRulesProtected 'The race fixture inherits permissions.'
    Assert-Condition (@($raceAcl.Access | Where-Object {
        $_.AccessControlType -eq 'Allow' -and
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $currentSid.Value
    }).Count -eq 0) 'The race fixture permits a foreign account.'
    $raceCode = $pythonCode.Replace(
        'fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)',
        "Path('/credentials/admin-hash.env').write_text('RACE_SENTINEL\n', encoding='ascii')`ntry:`n    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)`nexcept FileExistsError:`n    raise SystemExit(73)"
    )
    Assert-Condition ($raceCode.Contains("write_text('RACE_SENTINEL\n'")) 'The fixed-writer race seam was not installed.'
    Assert-Condition ($raceCode.Contains('except FileExistsError:')) 'The FileExistsError race seam was not installed.'
    $raceHash = Join-Path $raceDir 'admin-hash.env'
    $raceExit = Invoke-RealPython $imageId ([IO.Path]::GetFullPath($raceDir)) $raceCode
    Assert-Condition ($raceExit -eq 73) 'The real Docker race did not map FileExistsError to exit 73.'
    Assert-Condition (([IO.File]::ReadAllText($raceHash)) -eq "RACE_SENTINEL`n") 'The FileExistsError race changed the sentinel.'

    $LASTEXITCODE = 0
    Write-Output ('PASS: setup-admin-password regression (' + $Runner + ')')
}
finally {
    if ($fixtureCreated) {
        $cleanupCache = [IO.Path]::GetFullPath($workcacheRoot).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
        $cleanupFixture = [IO.Path]::GetFullPath($fixtureRoot)
        $cleanupPrefix = $cleanupCache + [IO.Path]::DirectorySeparatorChar
        Assert-Condition ($cleanupFixture.StartsWith($cleanupPrefix, [StringComparison]::OrdinalIgnoreCase)) 'Cleanup path escaped .workcache.'
        Assert-Condition ((Split-Path -Leaf $cleanupFixture) -eq $fixtureName) 'Cleanup path has an unexpected fixture name.'
        Assert-Condition (Test-Path -LiteralPath $markerPath -PathType Leaf) 'Cleanup marker is missing; refusing recursive cleanup.'
        Assert-Condition (([IO.File]::ReadAllText($markerPath)) -eq $markerValue) 'Cleanup marker does not belong to this test run.'
        Remove-Item -LiteralPath $cleanupFixture -Recurse -Force -ErrorAction Stop
        Assert-Condition (-not (Test-Path -LiteralPath $cleanupFixture)) 'The test fixture was not removed.'
        Write-Output ('PASS: isolated cleanup (' + $Runner + ')')
    }
}
