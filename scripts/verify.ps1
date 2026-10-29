[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repositoryRoot

$projectName = 'smokeline-e2e'
$restoreProjectName = 'smokeline-restore-verify'
$phase2TestHash = 'JGFyZ29uMmlkJHY9MTkkbT02NTUzNix0PTMscD0yJFN3R3d6dEI3UnB4NkwwdnhydXR4SlEkeDdaYjJYbEFaajExMG1qbHF2bFJISzIvZVNNMW9aQTVsckNUVElZRlZJTQ=='
$compose = @(
    'compose',
    '-p', $projectName,
    '-f', 'compose.yml',
    '-f', 'compose.test.yml'
)
$started = $false
$restoreCleanupNeeded = $false
$restoreCompose = @()
$previousTestProvider = $env:TEST_AI_PROVIDER
$previousRestoreProject = $env:RESTORE_PROJECT
$env:TEST_AI_PROVIDER = 'fake'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$Executable = 'docker'
    )

    Write-Host "==> $Label"
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Command
    )

    Invoke-Checked -Label $Label -Arguments ($compose + $Command)
}

function Wait-Healthy {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [string[]]$ComposeArguments = $compose
    )

    $deadline = (Get-Date).AddMinutes(3)
    while ((Get-Date) -lt $deadline) {
        $containerId = ((& docker @ComposeArguments ps -q $Service) -join '').Trim()
        if ($containerId) {
            $status = (& docker inspect --format '{{.State.Health.Status}}' $containerId).Trim()
            if ($status -eq 'healthy') {
                Write-Host "healthy: $Service"
                return
            }
            $state = (& docker inspect --format '{{.State.Status}}' $containerId).Trim()
            if ($state -eq 'exited' -or $state -eq 'dead') {
                throw "$Service stopped before becoming healthy"
            }
        }
        Start-Sleep -Seconds 2
    }
    throw "timed out waiting for $Service"
}

function Assert-ComposeProjectLabels {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectName,
        [switch]$RequireEmpty
    )
    $checks = @(
        @{ Kind = 'container'; List = @('ps', '-a', '--filter', "label=com.docker.compose.project=$ProjectName", '--format', '{{.Names}}'); LabelFormat = '{{json .Config.Labels}}' },
        @{ Kind = 'volume'; List = @('volume', 'ls', '--filter', "label=com.docker.compose.project=$ProjectName", '--format', '{{.Name}}'); LabelFormat = '{{json .Labels}}' },
        @{ Kind = 'network'; List = @('network', 'ls', '--filter', "label=com.docker.compose.project=$ProjectName", '--format', '{{.Name}}'); LabelFormat = '{{json .Labels}}' }
    )
    $resourceCount = 0
    foreach ($check in $checks) {
        $listArguments = $check.List
        $names = @(& docker @listArguments)
        if ($LASTEXITCODE -ne 0) { throw "cannot inspect $($check.Kind) inventory" }
        foreach ($name in ($names | Where-Object { $_ -and $_.Trim() })) {
            $inspectArguments = @($check.Kind, 'inspect', '--format', $check.LabelFormat, $name)
            $labelJson = @(& docker @inspectArguments)
            $inspectExitCode = $LASTEXITCODE
            $labelObject = $null
            if ($inspectExitCode -eq 0 -and $labelJson.Count -gt 0) {
                try {
                    $labelObject = ($labelJson -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop
                } catch {
                    $labelObject = $null
                }
            }
            $label = $null
            if ($null -ne $labelObject) {
                $labelProperty = $labelObject.PSObject.Properties['com.docker.compose.project']
                if ($null -ne $labelProperty) {
                    $label = [string]$labelProperty.Value
                }
            }
            if ($inspectExitCode -ne 0 -or $label -ne $ProjectName) {
                throw "refusing test resource changes: unexpected owner for $name"
            }
            $resourceCount++
        }
    }
    if ($RequireEmpty -and $resourceCount -gt 0) {
        throw "$ProjectName resources already exist; inspect and explicitly resolve them before running verification. Nothing was removed."
    }
}

function Assert-TestProjectLabels {
    param([switch]$RequireEmpty)
    $checks = @(
        @{ Kind = 'container'; List = @('ps', '-a', '--format', '{{.Names}}'); LabelFormat = '{{json .Config.Labels}}' },
        @{ Kind = 'volume'; List = @('volume', 'ls', '--format', '{{.Name}}'); LabelFormat = '{{json .Labels}}' },
        @{ Kind = 'network'; List = @('network', 'ls', '--format', '{{.Name}}'); LabelFormat = '{{json .Labels}}' }
    )
    $resourceCount = 0
    foreach ($check in $checks) {
        $listArguments = $check.List
        $names = @(& docker @listArguments)
        if ($LASTEXITCODE -ne 0) { throw "cannot inspect $($check.Kind) inventory" }
        foreach ($name in ($names | Where-Object { $_ -match ('^' + [regex]::Escape($projectName) + '[-_]') })) {
            $inspectArguments = @($check.Kind, 'inspect', '--format', $check.LabelFormat, $name)
            $labelJson = @(& docker @inspectArguments)
            $inspectExitCode = $LASTEXITCODE
            $labelObject = $null
            if ($inspectExitCode -eq 0 -and $labelJson.Count -gt 0) {
                try {
                    $labelObject = ($labelJson -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop
                } catch {
                    $labelObject = $null
                }
            }
            $label = $null
            if ($null -ne $labelObject) {
                $labelProperty = $labelObject.PSObject.Properties['com.docker.compose.project']
                if ($null -ne $labelProperty) {
                    $label = [string]$labelProperty.Value
                }
            }
            if ($inspectExitCode -ne 0 -or $label -ne $projectName) {
                throw "refusing test resource changes: unexpected owner for $name"
            }
            $resourceCount++
        }
    }
    if ($RequireEmpty -and $resourceCount -gt 0) {
        throw 'smokeline-e2e resources already exist; inspect and explicitly resolve them before running verification. Nothing was removed.'
    }
}

try {
    $bufferGuard = Join-Path $repositoryRoot 'scripts/tests/nginx-transcription-buffer.tests.ps1'
    Invoke-Checked -Label 'Nginx transcription buffer static guard' -Executable 'pwsh.exe' -Arguments @('-NoLogo', '-NoProfile', '-File', $bufferGuard)
    Invoke-Checked -Label 'Docker server' -Arguments @('info', '--format', '{{.ServerVersion}}')
    Invoke-Checked -Label 'Compose' -Arguments @('compose', 'version')
    Invoke-Compose -Label 'Compose config' -Command @('config', '--quiet')
    Assert-TestProjectLabels -RequireEmpty

    # Mark the first resource-creating Compose call before it runs so any
    # later failure still enters the exact-label cleanup path.
    $started = $true
    Invoke-Compose -Label 'Build isolated test images' -Command @('build', 'backend', 'nginx', 'test-runner')
    $evalInput = Join-Path $repositoryRoot 'evals/query_cases.example.jsonl'
    Invoke-Compose -Label 'Synthetic text evaluation schema check' -Command @(
        'run', '--rm', '--no-deps',
        '-v', "$evalInput`:/tmp/query_cases.example.jsonl:ro",
        'backend', 'python', '-m', 'app.scripts.run_evals',
        '--input', '/tmp/query_cases.example.jsonl', '--validate-only'
    )
    Invoke-Compose -Label 'Start isolated PostgreSQL' -Command @('up', '-d', 'postgres')
    Wait-Healthy -Service 'postgres'

    Invoke-Compose -Label 'Apply initial migration' -Command @('run', '--rm', 'backend', 'alembic', 'upgrade', 'head')
    Invoke-Compose -Label 'Downgrade migration to base' -Command @('run', '--rm', 'backend', 'alembic', 'downgrade', 'base')
    Invoke-Compose -Label 'Re-apply initial migration' -Command @('run', '--rm', 'backend', 'alembic', 'upgrade', 'head')

    Invoke-Compose -Label 'Backend format check' -Command @('run', '--rm', '--no-deps', '-e', 'RUFF_CACHE_DIR=/data/tmp/ruff-cache', 'backend', 'ruff', 'format', '--check', '.')
    Invoke-Compose -Label 'Backend lint' -Command @('run', '--rm', '--no-deps', '-e', 'RUFF_CACHE_DIR=/data/tmp/ruff-cache', 'backend', 'ruff', 'check', '.')
    $evalDirectory = Join-Path $repositoryRoot 'evals'
    $scriptsDirectory = Join-Path $repositoryRoot 'scripts'
    $coverageGate = @'
set -eu
python -m pytest -p no:cacheprovider --basetemp=/data/tmp/pytest-tmp --cov=app --cov-report=term-missing
python /opt/smokeline/scripts/tests/backend_coverage_gate.py --data-file /data/tmp/.coverage
'@
    Invoke-Compose -Label 'Backend tests and explicit coverage gate' -Command @(
        'run', '--rm', '--no-deps',
        '-v', "$evalDirectory`:/opt/smokeline/evals:ro",
        '-v', "$scriptsDirectory`:/opt/smokeline/scripts:ro",
        '-e', 'APP_ENV=test',
        '-e', 'ALLOWED_ORIGINS=http://localhost:8080,http://localhost:5173',
        '-e', 'AI_PROVIDER=disabled',
        '-e', 'PUBLIC_BASE_URL=http://localhost:8080',
        '-e', "ADMIN_PASSWORD_HASH_B64=$phase2TestHash",
        '-e', 'PHASE2_INTEGRATION_REQUIRED=1',
        '-e', 'COVERAGE_FILE=/data/tmp/.coverage',
        'backend', 'sh', '-ec', $coverageGate
    )

    $productionCompose = @('compose', '-p', 'smokeline-production-build', '-f', 'compose.yml')
    Invoke-Checked -Label 'Build production images independently' -Arguments ($productionCompose + @('build', 'backend', 'nginx'))

    if (-not (Test-Path (Join-Path $repositoryRoot 'frontend/node_modules'))) {
        throw 'frontend/node_modules is missing; run npm ci in frontend before verify.ps1'
    }
    Push-Location (Join-Path $repositoryRoot 'frontend')
    try {
        Invoke-Checked -Label 'Frontend lint' -Executable 'npm' -Arguments @('run', 'lint')
        Invoke-Checked -Label 'Frontend typecheck' -Executable 'npm' -Arguments @('run', 'typecheck')
        Invoke-Checked -Label 'Frontend tests and coverage' -Executable 'npm' -Arguments @('run', 'test:coverage')
        Invoke-Checked -Label 'Frontend production build' -Executable 'npm' -Arguments @('run', 'build')
    }
    finally {
        Pop-Location
    }

    Invoke-Compose -Label 'Start complete isolated stack' -Command @('up', '-d', 'backend', 'nginx')
    Wait-Healthy -Service 'backend'
    Wait-Healthy -Service 'nginx'
    Invoke-Compose -Label 'Nginx syntax check' -Command @('exec', '-T', 'nginx', 'nginx', '-t', '-c', '/etc/nginx/nginx.conf')
    $nginxLogsBeforeTranscriptionProbe = (& docker @compose logs --no-color nginx 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { throw 'cannot read isolated Nginx logs before transcription probe' }
    $transcriptionProbe = @'
import http.client
import json

boundary = b"----SmokeLineTranscriptionBufferProbe"
preamble = (
    b"--" + boundary + b"\r\n"
    b"Content-Disposition: form-data; name=\"audio\"; filename=\"near-limit.wav\"\r\n"
    b"Content-Type: audio/wav\r\n\r\n"
)
audio = b"0" * (5 * 1024 * 1024 + 8192)
suffix = b"\r\n--" + boundary + b"--\r\n"
body = preamble + audio + suffix
assert len(audio) > 5 * 1024 * 1024
assert len(body) < 6 * 1024 * 1024

connection = http.client.HTTPConnection("nginx", 8080, timeout=30)
try:
    connection.request(
        "POST",
        "/api/v1/transcriptions",
        body=body,
        headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary.decode(),
            "Content-Length": str(len(body)),
        },
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
finally:
    connection.close()

assert response.status == 413, (response.status, payload)
assert payload["error"]["code"] == "AUDIO_TOO_LARGE", payload
print(f"PASS: application rejected {len(body)}-byte multipart with AUDIO_TOO_LARGE; Nginx accepted the <6MiB request")
'@
    $probeEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($transcriptionProbe))
    $probeCommand = "import base64; exec(base64.b64decode('$probeEncoded'))"
    Invoke-Compose -Label 'Near-limit transcription multipart probe' -Command @('exec', '-T', 'backend', 'python', '-c', $probeCommand)
    Write-Host '==> Nginx client temp directory check'
    $nginxTempEntries = @(& docker @compose exec -T nginx find /tmp/client_temp -mindepth 1 -print -quit)
    if ($LASTEXITCODE -ne 0) {
        throw 'cannot inspect the isolated Nginx client temp directory'
    }
    if ($nginxTempEntries.Count -gt 0) {
        throw 'isolated Nginx left a buffered request body in the client temp directory'
    }
    $nginxLogsAfterTranscriptionProbe = (& docker @compose logs --no-color nginx 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { throw 'cannot read isolated Nginx logs after transcription probe' }
    $warningBefore = ([regex]::Matches($nginxLogsBeforeTranscriptionProbe, 'buffered to a temporary file')).Count
    $warningAfter = ([regex]::Matches($nginxLogsAfterTranscriptionProbe, 'buffered to a temporary file')).Count
    if ($warningAfter -gt $warningBefore) {
        throw 'isolated Nginx emitted a client-body temporary-file warning during transcription probe'
    }
    # Phase 4 and 5A fake scenarios are appended to the existing Phase 2
    # serial journey, so fixtures are created once through visible admin UI
    # and then consumed by the same browser context.
    Invoke-Compose -Label 'Playwright Phase 1-5A fake E2E' -Command @('run', '--rm', '-e', 'TEST_AI_PROVIDER=fake', 'test-runner', 'npm', 'run', 'e2e', '--', 'phase1.spec.ts', 'phase2.spec.ts', 'phase5.spec.ts', '--workers=1')
    $env:TEST_AI_PROVIDER = 'disabled'
    try {
        Invoke-Compose -Label 'Recreate test backend with disabled provider' -Command @('up', '-d', '--force-recreate', 'backend')
        Wait-Healthy -Service 'backend'
        Invoke-Compose -Label 'Playwright Phase 4-5B disabled Web E2E' -Command @('run', '--rm', '-e', 'TEST_AI_PROVIDER=disabled', 'test-runner', 'npm', 'run', 'e2e', '--', 'phase4.spec.ts', 'phase5-disabled.spec.ts', 'phase5b.spec.ts', '--workers=1')
    }
    finally {
        $env:TEST_AI_PROVIDER = 'fake'
    }

    $perfSeedScript = Join-Path $repositoryRoot 'scripts/seed_phase5c_perf.py'
    Invoke-Compose -Label 'Seed 20 isolated published performance fixtures' -Command @(
        'run', '--rm', '--no-deps',
        '-v', "$perfSeedScript`:/tmp/seed_phase5c_perf.py:ro",
        '-e', 'AI_PROVIDER=disabled',
        '-e', 'PYTHONPATH=/opt/smokeline/backend',
        '-e', 'PHASE5C_PERF_SEED=1',
        'backend', 'python', '/tmp/seed_phase5c_perf.py'
    )

    $nginxMediaProbe = @'
import gzip
import http.client
import json
import re

def get(path, headers=None):
    connection = http.client.HTTPConnection("nginx", 8080, timeout=20)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response, body
    finally:
        connection.close()

search_body = json.dumps({
    "kind": "query",
    "text": "我在 T 出生点，想封警家烟",
    "conversation_id": None,
    "input_mode": "text",
}, ensure_ascii=False).encode("utf-8")
connection = http.client.HTTPConnection("nginx", 8080, timeout=20)
try:
    connection.request("POST", "/api/v1/search", body=search_body, headers={
        "Content-Type": "application/json",
        "Content-Length": str(len(search_body)),
        "X-Request-ID": "5c" * 16,
    })
    response = connection.getresponse()
    search_result = json.loads(response.read())
finally:
    connection.close()
assert response.status == 200 and search_result.get("type") == "match", search_result.get("type")
media_url = search_result["lineup"]["primary_video"]["url"]
media_path = media_url.split("?", 1)[0]
response, media_body = get(media_path, {"Range": "bytes=0-1023"})
assert response.status == 206, response.status
assert response.getheader("Content-Range", "").startswith("bytes 0-1023/"), response.getheaders()
assert len(media_body) == 1024, len(media_body)

response, index_body = get("/", {"Accept-Encoding": "identity"})
assert response.status == 200
assets = sorted(set(re.findall(rb"(?:src|href)=\"(/assets/[^\"]+\.(?:js|css))\"", index_body)))
assert assets, "no own JS/CSS assets"
compressed_bytes = 0
for asset in assets:
    response, body = get(asset.decode("ascii"), {"Accept-Encoding": "identity"})
    assert response.status == 200, asset
    compressed_bytes += len(gzip.compress(body, mtime=0))
assert compressed_bytes <= 500 * 1024, compressed_bytes
print(f"PASS: nginx Range=206 bytes=1024 gzip_js_css={compressed_bytes}")
'@
    $probeEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($nginxMediaProbe))
    $probeCommand = "import base64; exec(base64.b64decode('$probeEncoded'))"
    Invoke-Compose -Label 'Nginx Range and gzip asset gate' -Command @('exec', '-T', 'backend', 'python', '-c', $probeCommand)

    $loadScript = Join-Path $repositoryRoot 'scripts/smoke_load.py'
    $performanceOverride = ([System.IO.Path]::GetTempFileName())
    $previousPerformanceProvider = $env:TEST_AI_PROVIDER
    $env:TEST_AI_PROVIDER = 'disabled'
    try {
        [System.IO.File]::WriteAllText($performanceOverride, @'
services:
  backend:
    environment:
      DB_POOL_SIZE: "10"
      DB_MAX_OVERFLOW: "0"
      AI_PROVIDER: disabled
'@)
        $performanceCompose = @(
            'compose',
            '-p', $projectName,
            '-f', 'compose.yml',
            '-f', 'compose.test.yml',
            '-f', $performanceOverride
        )
        Invoke-Checked -Label 'Performance Compose config' -Arguments ($performanceCompose + @('config', '--quiet'))
        Invoke-Checked -Label 'Recreate performance backend with DB_POOL_SIZE=10' -Arguments ($performanceCompose + @('up', '-d', '--force-recreate', 'backend'))
        Wait-Healthy -Service 'backend' -ComposeArguments $performanceCompose
        Invoke-Checked -Label 'Deterministic local performance gate' -Arguments ($performanceCompose + @(
            'run', '--rm', '--no-deps',
            '-v', "$loadScript`:/tmp/smoke_load.py:ro",
            'backend', 'python', '/tmp/smoke_load.py', '--base-url', 'http://backend:8000',
            '--requests', '1000', '--concurrency', '20', '--warmup', '20'
        ))
        Invoke-Compose -Label 'Restore pool-one backend after performance' -Command @('up', '-d', '--force-recreate', 'backend')
        Wait-Healthy -Service 'backend'
    }
    finally {
        $env:TEST_AI_PROVIDER = $previousPerformanceProvider
        if (Test-Path -LiteralPath $performanceOverride) {
            Remove-Item -LiteralPath $performanceOverride -Force -ErrorAction SilentlyContinue
        }
    }

    Invoke-Compose -Label 'Same-lock backup gate' -Command @(
        'exec', '-T', 'backend', 'python', '-m', 'app.scripts.backup'
    )
    $backupGroup = ((& docker @compose exec -T backend sh -c 'cat /data/backups/latest') -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or $backupGroup -notmatch '^[0-9]{8}-[0-9]{6}$') {
        throw 'backup latest pointer is missing or unsafe'
    }
    $backupVolumes = @(& docker volume ls --filter "label=com.docker.compose.project=$projectName" --format '{{.Name}}')
    if ($LASTEXITCODE -ne 0) { throw 'cannot inspect test backup volume' }
    $backupVolume = ($backupVolumes | Where-Object { $_ -match 'backups$' } | Select-Object -First 1)
    if (-not $backupVolume) { throw 'test backup volume is missing' }
    $restoreComposeFile = Join-Path $repositoryRoot 'compose.restore.yml'
    $env:RESTORE_PROJECT = $restoreProjectName
    Invoke-Compose -Label 'Isolated restore manifest and scope gate' -Command @(
        'run', '--rm', '--no-deps',
        '-v', "$backupVolume`:/restore/input:ro",
        '-v', "$restoreComposeFile`:/tmp/compose.restore.yml:ro",
        '-e', "SMOKELINE_RESTORE_PROJECT=$restoreProjectName",
        'backend', 'python', '-m', 'app.scripts.restore',
        '--backup', "/restore/input/$backupGroup",
        '--project', 'smokeline-restore-verify',
        '--restore-root', '/data/tmp/restore',
        '--media-root', '/data/tmp/restore/media',
        '--compose-file', '/tmp/compose.restore.yml',
        '--port', '18080'
    )
    $restoreCompose = @(
        'compose',
        '-p', $restoreProjectName,
        '-f', 'compose.yml',
        '-f', 'compose.restore.yml'
    )
    Assert-ComposeProjectLabels -ProjectName $restoreProjectName -RequireEmpty
    Invoke-Checked -Label 'Restore Compose config' -Arguments ($restoreCompose + @('config', '--quiet'))
    $restoreCleanupNeeded = $true
    Invoke-Checked -Label 'Start isolated restore PostgreSQL' -Arguments ($restoreCompose + @('up', '-d', 'postgres'))
    Wait-Healthy -Service 'postgres' -ComposeArguments $restoreCompose
    Invoke-Checked -Label 'Prepare isolated restore volumes' -Arguments ($restoreCompose + @('create', 'backend'))
    $restoreMediaVolume = ((& docker volume ls --filter "label=com.docker.compose.project=$restoreProjectName" --filter 'label=com.docker.compose.volume=smokeline-restore-media-store' --format '{{.Name}}') -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $restoreMediaVolume) {
        throw 'isolated restore media volume is missing'
    }
    Invoke-Checked -Label 'Restore database and media into isolated volumes' -Arguments ($restoreCompose + @(
        'run', '--rm', '--no-deps',
        '-v', "$backupVolume`:/restore/input:ro",
        '-v', "$restoreMediaVolume`:/data/tmp/restore/media",
        '-v', "$restoreComposeFile`:/tmp/compose.restore.yml:ro",
        '-e', "SMOKELINE_RESTORE_PROJECT=$restoreProjectName",
        'backend', 'python', '-m', 'app.scripts.restore',
        '--backup', "/restore/input/$backupGroup",
        '--project', $restoreProjectName,
        '--restore-root', '/data/tmp/restore',
        '--media-root', '/data/tmp/restore/media',
        '--compose-file', '/tmp/compose.restore.yml',
        '--port', '18080',
        '--database-url', 'postgresql+psycopg://smokeline:smokeline@postgres:5432/smokeline',
        '--execute'
    ))
    Invoke-Checked -Label 'Start isolated restored Web stack' -Arguments ($restoreCompose + @('up', '-d', 'backend', 'nginx'))
    Wait-Healthy -Service 'backend' -ComposeArguments $restoreCompose
    Wait-Healthy -Service 'nginx' -ComposeArguments $restoreCompose
    $restorePort = ((& docker port "$restoreProjectName-nginx-1" '8080/tcp') -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or $restorePort -ne '127.0.0.1:18080') {
        throw "isolated restore Web port is not loopback-only: $restorePort"
    }
    Invoke-Checked -Label 'Restored isolated host Web ready' -Executable 'curl.exe' -Arguments @(
        '--fail', '--silent', '--show-error', '--max-time', '15',
        'http://127.0.0.1:18080/api/v1/readyz'
    )
    Invoke-Checked -Label 'Restored isolated Web E2E' -Arguments ($restoreCompose + @('run', '--rm', 'test-runner'))
    Assert-ComposeProjectLabels -ProjectName $restoreProjectName
    Write-Host 'PASS: restored isolated stack served the deterministic public route; remote HTTPS/Web acceptance remains BLOCKED.'
    Write-Host 'Local verification complete; remote HTTPS/Web acceptance remains BLOCKED.'
}
finally {
    $cleanupErrors = @()
    if ($restoreCleanupNeeded) {
        try {
            Assert-ComposeProjectLabels -ProjectName $restoreProjectName
            Invoke-Checked -Label 'Clean only isolated restore resources' -Arguments ($restoreCompose + @('down', '-v'))
        }
        catch {
            $cleanupErrors += "restore cleanup failed: $($_.Exception.Message)"
        }
    }
    if ($started) {
        try {
            Assert-TestProjectLabels
            Invoke-Compose -Label 'Clean only smokeline-e2e test resources' -Command @('down', '-v')
        }
        catch {
            $cleanupErrors += "e2e cleanup failed: $($_.Exception.Message)"
        }
    }
    if ($null -eq $previousTestProvider) {
        Remove-Item Env:TEST_AI_PROVIDER -ErrorAction SilentlyContinue
    }
    else {
        $env:TEST_AI_PROVIDER = $previousTestProvider
    }
    if ($null -eq $previousRestoreProject) {
        Remove-Item Env:RESTORE_PROJECT -ErrorAction SilentlyContinue
    }
    else {
        $env:RESTORE_PROJECT = $previousRestoreProject
    }
    if ($cleanupErrors.Count -gt 0) {
        throw ($cleanupErrors -join '; ')
    }
}
