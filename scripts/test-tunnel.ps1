$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$credentials = Get-Content -Raw -Encoding UTF8 (Join-Path $projectRoot '.local-secrets\cabinet-credentials.json') | ConvertFrom-Json
$authHeaders = @{Authorization='Basic '+[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($credentials.username+':'+$credentials.password))}
if (Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Local port 18080 is occupied; existing process will not be stopped.'
}
$verificationDir = Join-Path $projectRoot 'verification'
New-Item -ItemType Directory -Force $verificationDir | Out-Null
$tunnel = Start-Process -FilePath (Get-Command ssh.exe).Source -ArgumentList @(
    '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-N',
    '-L', '127.0.0.1:18080:127.0.0.1:8080', 'finkoper-ai'
) -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $verificationDir 'tunnel-stderr.txt')
try {
    $listening = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $tunnel.Refresh()
        if ($tunnel.HasExited) { throw 'SSH tunnel exited; inspect tunnel-stderr.txt' }
        if (Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue) {
            $listening = $true
            break
        }
        Start-Sleep -Milliseconds 300
    }
    if (-not $listening) { throw 'Tunnel startup timed out' }
    $lines = @()
    foreach ($check in @(
        @{path='healthz'; code=200; body='"status":"ok"'},
        @{path='readyz'; code=200; body='"status":"ready"'}
    )) {
        $url = "http://127.0.0.1:18080/$($check.path)"
        $response = Invoke-WebRequest -UseBasicParsing -Uri $url -Headers $authHeaders -TimeoutSec 10
        $responseText = $response.Content + "`nHTTP_STATUS=$($response.StatusCode)"
        if (-not $responseText.Contains("HTTP_STATUS=$($check.code)")) { throw $responseText }
        if (-not $responseText.Contains($check.body)) { throw $responseText }
        $lines += "PASS $url`n$responseText"
    }
    $lines += 'Temporary SSH tunnel closed after this verification.'
    [IO.File]::WriteAllText((Join-Path $verificationDir 'ssh-tunnel.txt'),
        ($lines -join "`n") + "`n", (New-Object Text.UTF8Encoding($false)))
    $lines
} finally {
    $tunnel.Refresh()
    if (-not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue }
    $tunnel.Dispose()
}
