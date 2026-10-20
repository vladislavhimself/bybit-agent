# Российская машина как ретранслятор: бот здесь не торгует, только пропускает запросы головы (немецкий сервер)
# к T-Invest, ISS и российским новостям через WireGuard-туннель. Запуск из папки проекта от администратора:
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_relay.ps1
# Перед этим должен быть установлен WireGuard (wireguard.com/install) с импортированным wg-bot.conf и включённым туннелем.
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root

$tun = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -eq "10.8.0.2" }
if (-not $tun) { Write-Host "ОШИБКА: туннель WireGuard не активен (нет адреса 10.8.0.2). Включите туннель wg-bot в WireGuard и запустите снова." -ForegroundColor Red; exit 1 }

# локальный бот больше не нужен: торгует голова в Германии
foreach ($t in @("BybitAgent")) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue; Disable-ScheduledTask -TaskName $t | Out-Null; Write-Host "Задача $t остановлена и отключена" }
}
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$root*" } | Stop-Process -Force -ErrorAction SilentlyContinue

$py = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { (Get-Command python).Source }
$task = "BybitRelay"
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$root\scripts\windows\relay.py`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $task -Confirm:$false }
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
# файрвол: порт ретранслятора открыт только для адресов туннеля
if (-not (Get-NetFirewallRule -Name "BybitRelay" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name "BybitRelay" -DisplayName "Bybit relay (WireGuard only)" -Direction Inbound -Protocol TCP -LocalPort 8080 -RemoteAddress 10.8.0.0/24 -Action Allow | Out-Null
}
Start-ScheduledTask -TaskName $task
Start-Sleep -Seconds 3
Write-Host "Задача $task : $((Get-ScheduledTask -TaskName $task).State). Ретранслятор слушает 10.8.0.2:8080 только из туннеля."
