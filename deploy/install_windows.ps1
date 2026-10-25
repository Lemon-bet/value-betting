# Instala vb en Windows en C:\OTRO\vb y lo registra como tarea programada que arranca con el sistema.
# Ejecutar en PowerShell como administrador, desde la carpeta donde está descomprimido el zip:
#   powershell -ExecutionPolicy Bypass -File deploy\install_windows.ps1
$ErrorActionPreference = "Stop"
$Root = "C:\OTRO\vb"
$Src  = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Write-Host "== Copiando a $Root"
New-Item -ItemType Directory -Force -Path $Root | Out-Null
robocopy $Src $Root /E /XD .venv data __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS | Out-Null
New-Item -ItemType Directory -Force -Path "$Root\data" | Out-Null

Write-Host "== Entorno virtual propio (no toca el Python de MAREA)"
Set-Location $Root
if (-not (Test-Path "$Root\.venv")) { python -m venv .venv }
& "$Root\.venv\Scripts\python.exe" -m pip install --quiet --upgrade pip requests

if (-not (Test-Path "$Root\.env")) {
    Copy-Item "$Root\.env.example" "$Root\.env"
    Write-Host "== Creado .env: EDITA $Root\.env y pon ODDSPAPI_KEY (y Telegram si quieres) antes de arrancar."
}

Write-Host "== Tarea programada 'VB Collector' (arranque con el sistema, reinicio si falla)"
$action  = New-ScheduledTaskAction -Execute "$Root\.venv\Scripts\python.exe" -Argument "-m vb run" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "VB Collector" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "== Copia diaria de la base de datos a las 05:00"
$bAction = New-ScheduledTaskAction -Execute "$Root\.venv\Scripts\python.exe" -Argument "deploy\backup.py" -WorkingDirectory $Root
$bTrigger = New-ScheduledTaskTrigger -Daily -At 5:00
Register-ScheduledTask -TaskName "VB Backup" -Action $bAction -Trigger $bTrigger -Principal $principal -Force | Out-Null

Write-Host ""
Write-Host "Listo. Pasos siguientes (en $Root):"
Write-Host "  .venv\Scripts\python -m vb check        # no gasta cuota"
Write-Host "  .venv\Scripts\python -m vb markets      # 1 consulta"
Write-Host "  .venv\Scripts\python -m vb fixtures     # 1 consulta por competición"
Write-Host "  Start-ScheduledTask -TaskName 'VB Collector'"
Write-Host "  Get-Content data\vb.log -Wait          # ver el log"
