# 注册 Windows 定时任务「TenderAlert」：每天 10:00、15:00、22:00 各运行一次（2026-11-04 用户确认）。
# 综合采购一次最多只能看最新 100 条，跑得勤一点就不容易漏（见 README「关于综合采购」）。
# 电脑睡眠时到点会自己醒来运行（2026-11-05 用户要求），前提是 Windows 电源设置里「允许使用唤醒定时器」已启用。
# 没唤醒成功或关机了的话，开机后会补跑一次。
# 用 pythonw.exe 直接运行，不会弹出终端窗口；运行情况看 logs\tender-alert.log。
#
# 注册：pwsh -File install-task.ps1
# 查看：Get-ScheduledTask -TaskName TenderAlert
# 删除：Unregister-ScheduledTask -TaskName TenderAlert -Confirm:$false

$pythonw = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) {
    Write-Error "找不到 $pythonw，请先按 README 创建 .venv"
    exit 1
}
$action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m tender_alert' -WorkingDirectory $PSScriptRoot
$triggers = '10:00', '15:00', '22:00' | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 45)

Register-ScheduledTask -TaskName 'TenderAlert' -Description '上海保险招标提醒' `
    -Action $action -Trigger $triggers -Settings $settings -Force | Out-Null
Write-Host '已注册定时任务 TenderAlert：每天 10:00、15:00、22:00 各运行一次'
