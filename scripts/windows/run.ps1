# Цикл запуска бота: при любом завершении (ошибка, /update, /market) ждёт 10 с и запускает заново.
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
while ($true) {
    & "$root\.venv\Scripts\python.exe" -m bot.main 2>> "$root\logs\stderr.log"
    Start-Sleep -Seconds 10
}
