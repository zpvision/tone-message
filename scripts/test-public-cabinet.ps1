$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$credentials=Get-Content -Raw -Encoding UTF8 (Join-Path $root '.local-secrets\cabinet-credentials.json') | ConvertFrom-Json
$headers=@{Authorization='Basic '+[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($credentials.username+':'+$credentials.password))}
$results=@()
foreach($path in @('/','/api/config','/api/jobs','/api/prompts','/healthz','/readyz','/static/cabinet.js')) {
    try { $response=Invoke-WebRequest -UseBasicParsing -Uri ('https://ai.example.com'+$path) -TimeoutSec 10; $code=$response.StatusCode }
    catch { $code=[int]$_.Exception.Response.StatusCode }
    if($code -ne 401){throw "Auth bypass at $path"}
    $results+="$path anonymous=401"
}
$response=Invoke-WebRequest -UseBasicParsing -Uri 'https://ai.example.com/' -Headers $headers -TimeoutSec 10
if($response.StatusCode -ne 200){throw 'Authenticated page failed'}
$results+='Authenticated page=200'
$jobs=Invoke-RestMethod -Uri 'https://ai.example.com/api/jobs' -Headers $headers -TimeoutSec 10
$id=($jobs | Where-Object export_ready | Select-Object -First 1).id
foreach($kind in @('xlsx','jsonl','metrics')) {
    $url="https://ai.example.com/api/jobs/$id/download/$kind"
    try{$response=Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 10;$code=$response.StatusCode}
    catch{$code=[int]$_.Exception.Response.StatusCode}
    if($code -ne 401){throw 'Download bypass'}
    $response=Invoke-WebRequest -UseBasicParsing -Uri $url -Headers $headers -TimeoutSec 10
    if($response.StatusCode -ne 200){throw 'Authenticated download failed'}
    $results+="download/${kind}: anonymous=401, authenticated=200"
}
foreach($port in @(8000,8080,11434)) {
    $tcp=New-Object Net.Sockets.TcpClient
    try {
        $attempt=$tcp.BeginConnect('203.0.113.10',$port,$null,$null)
        $connected=$attempt.AsyncWaitHandle.WaitOne(2000) -and $tcp.Connected
        if($connected){throw "Unexpected public port $port"}
        $results+="Public port $port unavailable"
    } finally { $tcp.Dispose() }
}
[IO.File]::WriteAllText((Join-Path $root 'verification\cabinet-public-access.txt'),($results -join "`n")+"`n",(New-Object Text.UTF8Encoding($false)))
$results
