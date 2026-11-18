@echo off
setlocal
cd /d "%~dp0"

set "PEAKCUT_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PEAKCUT_PY%" (
  echo Nie znaleziono Pythona PeakCut w folderze .venv.
  pause
  exit /b 1
)

powershell.exe -NoProfile -Command "$rows = netstat -ano -p tcp | Select-String ':8765\s+.*LISTENING\s+(\d+)$'; foreach ($row in $rows) { $processId = [int]([regex]::Match($row.Line, 'LISTENING\s+(\d+)$').Groups[1].Value); $server = Get-Process -Id $processId -ErrorAction SilentlyContinue; if (-not $server -or $server.ProcessName -notmatch '^pythonw?$') { Write-Error 'Port 8765 jest zajety przez inna aplikacje; nie zatrzymalem jej.'; exit 2 }; Stop-Process -Id $processId -Force -ErrorAction Stop }; exit 0"
if errorlevel 1 (
  echo Nie udalo sie bezpiecznie uruchomic PeakCut.
  echo Sprawdz czy port 8765 nie jest zajety przez inna aplikacje.
  pause
  exit /b 1
)

start "PeakCut" /min /D "%~dp0" "%PEAKCUT_PY%" -m studio.server
timeout /t 4 /nobreak >nul
start "" "http://127.0.0.1:8765/app"
endlocal
