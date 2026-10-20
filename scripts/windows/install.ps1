# Установка бота на Windows: виртуальное окружение, зависимости, автозапуск через планировщик задач.
# Запуск из папки проекта в PowerShell от администратора:
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# Повторный запуск безопасен: обновляет зависимости и перезапускает бота.
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
Write-Host "Папка бота: $root"

if (-not (Test-Path "$root\.env")) {
    Write-Host "ОШИБКА: нет файла .env в $root. Положите присланный файл .env рядом с config.yaml и запустите снова." -ForegroundColor Red
    exit 1
}

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Host "ОШИБКА: python не найден. Установите Python 3.12 с python.org с галочкой 'Add python.exe to PATH'." -ForegroundColor Red; exit 1 }
$ver = & python -c "import sys; print(str(sys.version_info[0]) + '.' + str(sys.version_info[1]))"
Write-Host "Python $ver"
if ([version]$ver -lt [version]"3.11") { Write-Host "ОШИБКА: нужен Python 3.11 или новее." -ForegroundColor Red; exit 1 }

if (-not (Test-Path "$root\.venv\Scripts\python.exe")) { & python -m venv "$root\.venv" }
& "$root\.venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$root\.venv\Scripts\python.exe" -m pip install -q -r "$root\requirements.txt"
New-Item -ItemType Directory -Force -Path "$root\data", "$root\logs" | Out-Null
if (-not (Test-Path "$root\data\market.txt")) { Set-Content -Path "$root\data\market.txt" -Value "moex" -NoNewline -Encoding ascii }

# Задача планировщика: запуск при старте системы от SYSTEM, без входа пользователя; run.ps1 перезапускает бота при падении.
$task = "BybitAgent"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\windows\run.ps1`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $task -Confirm:$false }
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$root*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $task
Start-Sleep -Seconds 5
$state = (Get-ScheduledTask -TaskName $task).State
Write-Host "Задача $task : $state. Лог: $root\logs\bot.log. Через минуту в Telegram придёт сообщение «Бот запущен»."
