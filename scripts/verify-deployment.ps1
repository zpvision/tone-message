$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    $files = @('README.md', 'SETUP-REPORT.md', '.env.example', '.gitignore',
        'requirements.in', 'requirements.txt', 'requirements.lock.txt',
        'installed-python-packages.json','API-INTEGRATION.md','api-system-prompt.txt')
    $files += Get-ChildItem app,deploy,scripts,examples -Recurse -File |
        Where-Object { $_.FullName -notmatch '__pycache__' } |
        ForEach-Object { $_.FullName.Substring($projectRoot.Length + 1).Replace('\', '/') }
    $entries = @($files | ForEach-Object {
        if ($_ -notmatch '^[a-zA-Z0-9_./-]+$') { throw "Unsupported path: $_" }
        [ordered]@{ local = $_; remote = "/opt/finkoper-ai/$_";
            sha256 = (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant() }
    })
    $entries += @(
        @{ local='deploy/finkoper-ai.service'; remote='/etc/systemd/system/finkoper-ai.service' },
        @{ local='deploy/nginx-cabinet.conf'; remote='/etc/nginx/sites-available/finkoper-ai' },
        @{ local='deploy/journald-finkoper-ai.conf'; remote='/etc/systemd/journald.conf.d/finkoper-ai.conf' },
        @{ local='deploy/ollama.service'; remote='/etc/systemd/system/ollama.service' },
        @{ local='deploy/ollama-override.conf'; remote='/etc/systemd/system/ollama.service.d/finkoper-ai.conf' },
        @{ local='deploy/nginx-api-proxy.conf'; remote='/etc/nginx/snippets/finkoper-ai-api.conf' },
        @{ local='deploy/nginx-cabinet-proxy.conf'; remote='/etc/nginx/snippets/finkoper-ai-cabinet.conf' },
        @{ local='deploy/renew-nginx.sh'; remote='/etc/letsencrypt/renewal-hooks/deploy/finkoper-nginx' }
    ) | ForEach-Object {
        [ordered]@{ local=$_.local; remote=$_.remote;
            sha256=(Get-FileHash -LiteralPath $_.local -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
    $remotePaths = ($entries | ForEach-Object { $_.remote }) -join ' '
    $remoteOutput = @(ssh -o BatchMode=yes finkoper-ai "sudo -n sha256sum $remotePaths")
    if ($LASTEXITCODE -ne 0) { throw 'Remote hash command failed' }
    $remoteHashes = @{}
    foreach ($line in $remoteOutput) {
        if ($line -notmatch '^([a-f0-9]{64})\s+(.+)$') { throw "Unexpected sha256 output: $line" }
        $remoteHashes[$Matches[2]] = $Matches[1]
    }
    foreach ($entry in $entries) {
        if ($entry.sha256 -ne $remoteHashes[$entry.remote]) { throw "Hash mismatch: $($entry.remote)" }
    }
    $manifest = [ordered]@{
        server='finkoper-ai'; verified_at_utc=[DateTime]::UtcNow.ToString('o'); files=$entries
    } | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText((Join-Path $projectRoot 'deployment-manifest.json'),
        $manifest + "`n", (New-Object Text.UTF8Encoding($false)))
    Write-Output "PASS: $($entries.Count) deployed file hashes match local files."
} finally {
    Pop-Location
}
