@echo off
setlocal EnableExtensions
title BioAgri Process Simulator V0.20 Alpha

set "ROOT=%~dp0"
set "FRONTEND_SOURCE=%ROOT%frontend"
set "FRONTEND=%LOCALAPPDATA%\BioAgri Process Simulator\frontend-runtime"

echo ==========================================
echo BioAgri Process Simulator V0.20 Alpha
echo One-click launcher
echo ==========================================
echo.

if not exist "%ROOT%api_v020.py" (
    echo ERROR: api_v020.py is missing.
    pause
    exit /b 1
)
if not exist "%FRONTEND_SOURCE%\package.json" (
    echo ERROR: frontend\package.json is missing.
    pause
    exit /b 1
)

where python >nul 2>nul || (
    echo ERROR: Python is not installed or is not on PATH.
    pause
    exit /b 1
)
where node >nul 2>nul || (
    echo ERROR: Node.js is not installed or is not on PATH.
    pause
    exit /b 1
)
where npm >nul 2>nul || (
    echo ERROR: npm was not found.
    pause
    exit /b 1
)

echo [1/6] Stopping previous simulator processes...
for %%P in (8000 5173) do (
    powershell -NoProfile -Command "$ids = Get-NetTCPConnection -State Listen -LocalPort %%P -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; if ($ids) { Stop-Process -Id $ids -Force -ErrorAction SilentlyContinue }"
)
powershell -NoProfile -Command "$limit=(Get-Date).AddSeconds(15); do { $busy=Get-NetTCPConnection -State Listen -LocalPort 8000,5173 -ErrorAction SilentlyContinue; if (-not $busy) { exit 0 }; Start-Sleep -Milliseconds 300 } while ((Get-Date) -lt $limit); exit 1"
if errorlevel 1 (
    echo ERROR: Ports 8000 or 5173 could not be released.
    pause
    exit /b 1
)

echo [2/6] Installing/checking Python dependencies...
pushd "%ROOT%"
python -m pip install --disable-pip-version-check -r requirements.txt fastapi uvicorn
if errorlevel 1 (
    echo ERROR: Python dependencies could not be installed.
    popd
    pause
    exit /b 1
)
popd

echo [3/6] Preparing the frontend runtime...
if not exist "%FRONTEND%" mkdir "%FRONTEND%"
robocopy "%FRONTEND_SOURCE%" "%FRONTEND%" /E /PURGE /XD node_modules dist /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo ERROR: Frontend files could not be copied to the local runtime.
    pause
    exit /b 1
)
pushd "%FRONTEND%"
set "NEED_NPM_INSTALL=0"
if not exist "node_modules\vite\bin\vite.js" set "NEED_NPM_INSTALL=1"
if exist "node_modules\vite\bin\vite.js" for %%I in ("node_modules\vite\bin\vite.js") do if %%~zI EQU 0 set "NEED_NPM_INSTALL=1"
if "%NEED_NPM_INSTALL%"=="1" (
    echo Frontend dependencies are missing or damaged - reinstalling...
    if exist "package-lock.json" (
        call npm ci
    ) else (
        call npm install
    )
    if errorlevel 1 (
        echo ERROR: Frontend dependency installation failed.
        popd
        pause
        exit /b 1
    )
) else (
    echo Frontend dependencies are ready.
)
popd

echo [4/6] Starting V0.20 backend on port 8000...
start "BioAgri Backend" cmd /k "cd /d ""%ROOT%"" && python -m uvicorn api_v020:app --host 127.0.0.1 --port 8000"

echo [5/6] Starting frontend on port 5173...
start "BioAgri Frontend" cmd /k "cd /d ""%FRONTEND%"" && npm run dev -- --host 127.0.0.1 --port 5173"

echo [6/6] Waiting for the simulator to become ready...
powershell -NoProfile -Command "$limit=(Get-Date).AddSeconds(45); do { try { $api=(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/api/v020-meta' -TimeoutSec 2).StatusCode; $ui=(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:5173' -TimeoutSec 2).StatusCode; if ($api -eq 200 -and $ui -eq 200) { exit 0 } } catch {}; Start-Sleep -Seconds 1 } while ((Get-Date) -lt $limit); exit 1"
if errorlevel 1 (
    echo.
    echo ERROR: The simulator did not become ready within 45 seconds.
    echo Check the BioAgri Backend and BioAgri Frontend windows for details.
    pause
    exit /b 1
)

start "" "http://127.0.0.1:5173"
echo.
echo ==========================================
echo Simulator is running successfully.
echo Browser: http://127.0.0.1:5173
echo API:     http://127.0.0.1:8000/api/v020-meta
echo ==========================================
echo.
echo Keep the Backend and Frontend windows open.
echo Close them when you want to stop the simulator.
echo.
pause