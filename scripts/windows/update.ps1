# Обновление бота до последней версии из GitHub и перезапуск. Запуск из папки проекта от администратора:
#   powershell -ExecutionPolicy Bypass -File scripts\windows\update.ps1
# То же самое делает команда /update в Telegram.
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
& git pull --ff-only
& "$root\.venv\Scripts\python.exe" -m pip install -q -r "$root\requirements.txt"
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$root*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
if ((Get-ScheduledTask -TaskName "BybitAgent").State -ne "Running") { Start-ScheduledTask -TaskName "BybitAgent" }
Write-Host "Обновлено и перезапущено."
