param(
    [Parameter(Mandatory = $true)][string]$ProcessRecord,
    [ValidateSet('All', 'api', 'nginx', 'iam')][string]$Role = 'All'
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
    $recordedStart = ([datetime]$record.started).ToUniversalTime()
    if ([Math]::Abs(($launcher.CreationDate.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 2 -or
        !$launcher.ExecutablePath.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Process identity changed for role $($record.role); left it running"
    }
    # 递归收集 worker 与 Python 启动器；父进程先停止，避免退出期间重新拉起 worker。
    $targets = [Collections.Generic.List[object]]::new()
    $targets.Add($launcher)
    for ($index = 0; $index -lt $targets.Count; $index++) {
        $parent = $targets[$index]
        foreach ($child in ($processes | Where-Object {
            $_.ParentProcessId -eq $parent.ProcessId -and $_.CreationDate -ge $parent.CreationDate
        })) {
            if ($child.ProcessId -notin $targets.ProcessId) { $targets.Add($child) }
        }
    }
    foreach ($target in $targets) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($target.ProcessId)"
        if ($current -and $current.CreationDate -eq $target.CreationDate) {
            Stop-Process -Id $target.ProcessId -ErrorAction SilentlyContinue
        }
    }
    Write-Output "Stopped recorded role $($record.role)."
}
