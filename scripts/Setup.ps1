$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $projectRoot
python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败，请确认已安装 Python 3.11 或更新版本。' }
& '.\.venv\Scripts\python.exe' -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查上方错误。' }
Write-Host '安装完成。后续使用 .\.venv\Scripts\python.exe run.py ...'
