# 手动运行一次招标提醒。参数原样传给程序，例如：.\run.ps1 --dry-run
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Error "找不到 $python，请先按 README 创建 .venv"
    exit 1
}
& $python -m tender_alert @args
exit $LASTEXITCODE
