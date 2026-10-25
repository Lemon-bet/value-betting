# Lanza el backtest en segundo plano (no bloquea la consola ni el recolector).
# Uso: powershell -ExecutionPolicy Bypass -File C:\OTRO\vb\deploy\run_backtest.ps1
# Progreso: Get-Content C:\OTRO\vb\data\backtest.log -Tail 5
param([string]$From = "2026-01-01", [string]$To = "2026-10-24", [int]$Max = 600)
$Root = "C:\OTRO\vb"
Set-Location $Root
$env:VB_LOG = "data\backtest.log"   # log separado para no pisar el del recolector
Start-Process -FilePath "$Root\.venv\Scripts\python.exe" `
    -ArgumentList "-m vb backtest $From $To $Max" `
    -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput "$Root\data\backtest.out" -RedirectStandardError "$Root\data\backtest.err"
Write-Host "Backtest lanzado ($From a $To, max $Max partidos). Tarda unas 3 horas."
Write-Host "Progreso: Get-Content $Root\data\backtest.log -Tail 5"
