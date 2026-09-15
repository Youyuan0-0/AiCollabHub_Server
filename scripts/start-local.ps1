param(
    [Parameter(Mandatory = $true)][string]$NginxRoot,
    [ValidateRange(1, 65535)][int]$ApiPort = 18000,
    [ValidateRange(1, 32)][int]$Workers = 2,
    [ValidateRange(1, 65535)][int]$GatewayPort = 18880,
    [ValidateRange(0, 65535)][int]$MockIamPort = 0
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$gatewayRoot = (Resolve-Path -LiteralPath $NginxRoot).Path
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
$gateway = Join-Path $gatewayRoot 'nginx.exe'
if (!(Test-Path -LiteralPath $gateway)) { throw 'Nginx executable was not found' }
$ports = @($ApiPort, $GatewayPort)
if ($MockIamPort -gt 0) { $ports += $MockIamPort }
if (($ports | Select-Object -Unique).Count -ne $ports.Count) { throw 'All ports must differ' }
if (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $ports }) {
    throw 'A requested port is occupied. Stop the recorded services or choose unused ports.'
}
$runtime = Join-Path $projectRoot ('.runtime/local-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$recordFile = Join-Path $runtime 'processes.json'
$processes = @()
Push-Location $projectRoot
try {
    & $python -m scripts.render_nginx --nginx-root $gatewayRoot --api-port $ApiPort --port $GatewayPort
    if ($LASTEXITCODE -ne 0) { throw 'Gateway configuration generation failed' }
    Push-Location $gatewayRoot
    try { & $gateway -t; if ($LASTEXITCODE -ne 0) { throw 'Nginx configuration validation failed' } }
    finally { Pop-Location }
    $roles = @()
    if ($MockIamPort -gt 0) {
        $roles += [PSCustomObject]@{
            name='iam'
            arguments=@('-m','uvicorn','app.modules.common.auth.mock_iam:create_mock_iam','--factory','--host','127.0.0.1','--port',$MockIamPort,'--no-access-log')
        }
    }
    # 一套 API 共用一个监听端口；Uvicorn 管理 worker，每个进程独立创建连接池。
    $roles += [PSCustomObject]@{
        name='api'
        arguments=@('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',$ApiPort,'--workers',$Workers,'--no-access-log')
    }
    foreach ($role in $roles) {
        $process = Start-Process -FilePath $python -ArgumentList $role.arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime ($role.name + '.out.log')) -RedirectStandardError (Join-Path $runtime ($role.name + '.err.log')) -PassThru
        $processes += [PSCustomObject]@{ role=$role.name; pid=$process.Id; started=$process.StartTime.ToUniversalTime().ToString('o') }
        ConvertTo-Json -InputObject @($processes) | Set-Content -LiteralPath $recordFile -Encoding utf8
    }
    $nginx = Start-Process -FilePath $gateway -WorkingDirectory $gatewayRoot -WindowStyle Hidden -PassThru
    $processes += [PSCustomObject]@{ role='nginx'; pid=$nginx.Id; started=$nginx.StartTime.ToUniversalTime().ToString('o') }
    ConvertTo-Json -InputObject @($processes) | Set-Content -LiteralPath $recordFile -Encoding utf8
    Write-Output "User portal: http://127.0.0.1:$GatewayPort/applications"
    Write-Output "Admin portal: http://127.0.0.1:$GatewayPort/admin/"
    Write-Output "API: http://127.0.0.1:$ApiPort ($Workers workers)"
    Write-Output "Process records: $runtime"
}
catch {
    if (Test-Path -LiteralPath $recordFile) {
        & (Join-Path $PSScriptRoot 'stop-local.ps1') -ProcessRecord $recordFile
    }
    throw
}
finally { Pop-Location }
