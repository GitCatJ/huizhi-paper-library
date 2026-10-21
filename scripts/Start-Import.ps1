param(
    [string]$Source,
    [string]$Database,
    [ValidateRange(0,2147483647)][int]$Limit = 0,
    [string]$Resume
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $Database) { $Database = Join-Path $projectRoot 'data\library.sqlite3' }
if (-not $Resume -and -not $Source) { throw '请指定 -Source 试卷目录，或 -Resume 任务ID。' }
if ($Resume -and ($Source -or $Limit)) { throw '-Resume 不可同时指定 -Source 或 -Limit。' }
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { $pythonPath = (Get-Command python).Source }
$databasePath = [System.IO.Path]::GetFullPath($Database)
$launchLogDirectory = Join-Path (Split-Path $databasePath -Parent) 'logs'
New-Item -ItemType Directory -Path $launchLogDirectory -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$stdoutPath = Join-Path $launchLogDirectory "$stamp.stdout.log"
$stderrPath = Join-Path $launchLogDirectory "$stamp.stderr.log"
$arguments = @('-u', ('"' + (Join-Path $projectRoot 'run.py') + '"'), 'import', '--db', ('"' + $databasePath + '"'))
if ($Resume) {
    $arguments += @('--resume', $Resume)
} else {
    $sourcePath = (Resolve-Path -LiteralPath $Source).Path
    $arguments += @('--source', ('"' + $sourcePath + '"'), '--limit', "$Limit")
}
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
Write-Host "已启动本机后台进程 PID=$($process.Id)"
Write-Host "启动日志：$stdoutPath"
Write-Host "错误日志：$stderrPath"
Write-Host "进度命令：& `"$pythonPath`" `"$(Join-Path $projectRoot 'run.py')`" status --db `"$databasePath`""
Write-Host '完成判断：status 为 completed；completed_with_errors 表示有失败试卷或扫描问题，需要查看日志。'
