$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$configPaths = @(
    (Join-Path $repositoryRoot 'deploy/nginx.conf'),
    (Join-Path $repositoryRoot 'deploy/nginx.prod.conf')
)
$maxBodyBytes = 6 * 1024 * 1024

function Get-TranscriptionLocation {
    param([Parameter(Mandatory = $true)][string]$Path)

    $source = Get-Content -Raw -LiteralPath $Path
    $matches = [regex]::Matches(
        $source,
        '(?ms)^[ ]{8}location = /api/v1/transcriptions \{\r?\n(?<body>.*?)[ ]{8}\}'
    )
    if ($matches.Count -ne 1) {
        throw "$Path must contain exactly one exact transcription location"
    }
    return ($matches[0].Groups['body'].Value -replace "`r`n", "`n").TrimEnd()
}

$locations = @($configPaths | ForEach-Object { Get-TranscriptionLocation -Path $_ })
if ($locations[0] -ne $locations[1]) {
    throw 'development and production transcription locations diverge'
}

$requiredLines = @(
    'limit_req zone=transcription burst=2 nodelay;',
    'client_max_body_size 6m;',
    'client_body_buffer_size 6m;',
    'client_body_timeout 30s;',
    'proxy_connect_timeout 1500ms;',
    'proxy_read_timeout 30s;',
    'proxy_pass http://smokeline_backend;',
    'proxy_intercept_errors off;',
    'proxy_set_header Host $host;',
    'proxy_set_header X-Request-ID $request_id;'
)
foreach ($line in $requiredLines) {
    $pattern = '(?m)^[ ]{12}' + [regex]::Escape($line) + '$'
    if ([regex]::Matches($locations[0], $pattern).Count -ne 1) {
        throw "transcription location is missing or duplicates: $line"
    }
}

# A multipart body this close to the configured ceiling is larger than Nginx's
# default body buffer, but still fits the location's explicit in-memory buffer.
$boundary = '----SmokeLineBufferGuard'
$prefix = "--$boundary`r`nContent-Disposition: form-data; name=`"audio`"; filename=`"near-limit.wav`"`r`nContent-Type: audio/wav`r`n`r`n"
$suffix = "`r`n--$boundary--`r`n"
$representativeBytes = $maxBodyBytes - 4096
$envelopeBytes = [Text.Encoding]::UTF8.GetByteCount($prefix) + [Text.Encoding]::UTF8.GetByteCount($suffix)
if ($representativeBytes -le $envelopeBytes -or $representativeBytes -gt $maxBodyBytes) {
    throw 'representative multipart body calculation is invalid'
}

Write-Host "PASS: both Nginx transcription locations match; client body buffer and limit are 6 MiB; representative multipart length=$representativeBytes bytes is within the explicit buffer (static guard only; runtime replay is required)"
