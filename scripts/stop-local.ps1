param(
    [Parameter(Mandatory = $true)][string]$ProcessRecord,
    [ValidateSet('All', 'A', 'B', 'nginx')][string]$Role = 'All'
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$recordFile = (Resolve-Path -LiteralPath $ProcessRecord).Path
if (!$recordFile.StartsWith((Join-Path $projectRoot '.runtime') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The process record must belong to this project runtime directory'
}
$records = Get-Content -LiteralPath $recordFile -Raw | ConvertFrom-Json
$processes = Get-CimInstance Win32_Process
foreach ($record in $records) {
    if ($Role -ne 'All' -and $record.role -ne $Role) { continue }
    $launcher = $processes | Where-Object { $_.ProcessId -eq $record.pid }
    if (!$launcher) { continue }
    # 同时核对启动时间与路径，防止进程号被复用后误停其他程序。
    $recordedStart = ([datetime]$record.started).ToUniversalTime()
    if ([Math]::Abs(($launcher.CreationDate.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 2 -or
        !$launcher.ExecutablePath.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Process identity changed for role $($record.role); left it running"
    }
    foreach ($child in ($processes | Where-Object { $_.ParentProcessId -eq $launcher.ProcessId })) {
        Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $launcher.ProcessId -ErrorAction SilentlyContinue
    Write-Output "Stopped recorded role $($record.role)."
}
