param(
    [Parameter(Mandatory = $true)][string]$NginxRoot,
    [string]$ApplicationEnvFile,
    [int]$ApiPortA = 8000,
    [int]$ApiPortB = 8001,
    [int]$GatewayPort = 8080
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$gatewayRoot = (Resolve-Path -LiteralPath $NginxRoot).Path
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
$environmentFile = if ($ApplicationEnvFile) { (Resolve-Path -LiteralPath $ApplicationEnvFile).Path } else { $null }
$gateway = Join-Path $gatewayRoot 'nginx.exe'
if (!(Test-Path -LiteralPath $gateway)) { throw 'Nginx executable was not found' }
$ports = @($ApiPortA, $ApiPortB, $GatewayPort)
if (($ports | Select-Object -Unique).Count -ne 3) { throw 'All ports must differ' }
if (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $ports }) {
    throw 'A requested port is occupied. Choose unused ports; existing processes were not changed.'
}
$runtime = Join-Path $projectRoot ('.runtime/local-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
Push-Location $projectRoot
$previousInstanceId = $env:INSTANCE_ID
try {
    & $python -m scripts.render_nginx --nginx-root $gatewayRoot --api-a $ApiPortA --api-b $ApiPortB --port $GatewayPort
    if ($LASTEXITCODE -ne 0) { throw 'Gateway configuration generation failed' }
    Push-Location $gatewayRoot
    try { & $gateway -t; if ($LASTEXITCODE -ne 0) { throw 'Nginx configuration validation failed' } }
    finally { Pop-Location }
    $processes = @()
    foreach ($item in @(@('A', $ApiPortA), @('B', $ApiPortB))) {
        $env:INSTANCE_ID = $item[0]
        $arguments = @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',$item[1])
        if ($environmentFile) { $arguments += @('--env-file', ('"' + $environmentFile + '"')) }
        $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime ($item[0] + '.out.log')) -RedirectStandardError (Join-Path $runtime ($item[0] + '.err.log')) -PassThru
        $processes += [PSCustomObject]@{ role=$item[0]; pid=$process.Id; started=$process.StartTime.ToUniversalTime().ToString('o') }
    }
    $nginx = Start-Process -FilePath $gateway -WorkingDirectory $gatewayRoot -WindowStyle Hidden -PassThru
    $processes += [PSCustomObject]@{ role='nginx'; pid=$nginx.Id; started=$nginx.StartTime.ToUniversalTime().ToString('o') }
    $processes | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtime 'processes.json') -Encoding utf8
    Write-Output "User portal: http://127.0.0.1:$GatewayPort/feedback"
    Write-Output "Admin portal: http://127.0.0.1:$GatewayPort/admin/"
    Write-Output "Process records: $runtime"
}
finally {
    if ($null -eq $previousInstanceId) { Remove-Item Env:INSTANCE_ID -ErrorAction SilentlyContinue }
    else { $env:INSTANCE_ID = $previousInstanceId }
    Pop-Location
}
