@echo off
title CLIP AUTO Studio Launcher
setlocal EnableDelayedExpansion

if exist "%~dp0\.venv\Scripts\python.exe" (
    set "PROJECT_DIR=%~dp0"
) else (
    set "PROJECT_DIR=C:\Users\Roger\Documents\EXP\CLIP AUTO"
)
cd /d "%PROJECT_DIR%"

:LAUNCH
cls
echo ================================================================
echo               CLIP AUTO - AI Video Clipping Studio
echo ================================================================
echo.

:: 1. Check and start Ollama
echo [*] Checking Ollama AI Engine (Qwen 14B)...
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I /N "ollama.exe" >NUL
if "%ERRORLEVEL%"=="0" (
    echo [OK] Ollama is already running.
) else (
    echo [*] Starting Ollama background service...
    if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
        start "Ollama" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
    ) else (
        start "Ollama" /min ollama serve
    )
    ping -n 3 127.0.0.1 >nul
)

:: 2. Check virtual environment
if not exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found at:
    echo %PROJECT_DIR%\.venv
    echo.
    pause
    exit /b 1
)

:: 3. Check and start FastAPI Studio Server (Port 8765)
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul
if "%ERRORLEVEL%"=="0" (
    echo [OK] CLIP AUTO Studio is already running on port 8765.
) else (
    echo [*] Starting CLIP AUTO Studio on http://localhost:8765...
    start "CLIP_AUTO_FastAPI_8765" /min "%PROJECT_DIR%\.venv\Scripts\python.exe" server.py
)

:: 4. Check and start Streamlit UI (Port 8501)
netstat -ano | findstr ":8501" | findstr "LISTENING" >nul
if "%ERRORLEVEL%"=="0" (
    echo [OK] Streamlit UI is already running on port 8501.
) else (
    echo [*] Starting Streamlit UI on http://localhost:8501...
    start "CLIP_AUTO_Streamlit_8501" /min "%PROJECT_DIR%\.venv\Scripts\streamlit.exe" run app.py --server.port 8501 --server.headless true
)

:: 5. Wait for web services to initialize
echo [*] Waiting for web services to initialize...
ping -n 4 127.0.0.1 >nul

:: 6. Open Studio in default browser
echo [*] Opening CLIP AUTO Studio in default browser...
start http://localhost:8765

echo.
echo ================================================================
echo   CLIP AUTO is now LIVE and running!
echo.
echo   * Studio Web App:   http://localhost:8765
echo   * Streamlit App:    http://localhost:8501
echo   * Ollama AI Engine: http://localhost:11434
echo ================================================================
echo.
echo Controls:
echo   [S] Stop all CLIP AUTO servers and exit
echo   [R] Restart all CLIP AUTO servers
echo   [O] Open Studio in browser again
echo   [X] Exit this launcher (keep servers running in background)
echo.

:MENU_LOOP
choice /c SROX /n /m "Select [S]top, [R]estart, [O]pen browser, or e[X]it: "
if errorlevel 4 goto KEEP_EXIT
if errorlevel 3 goto OPEN_BROWSER
if errorlevel 2 goto RESTART_SERVERS
if errorlevel 1 goto STOP_SERVERS

:OPEN_BROWSER
echo [*] Opening browser...
start http://localhost:8765
goto MENU_LOOP

:RESTART_SERVERS
echo.
echo [*] Restarting all CLIP AUTO servers...
call :KILL_SERVERS
ping -n 3 127.0.0.1 >nul
goto LAUNCH

:STOP_SERVERS
echo.
echo [*] Stopping all CLIP AUTO servers...
call :KILL_SERVERS
echo [OK] All servers have been stopped.
ping -n 3 127.0.0.1 >nul
exit /b 0

:KEEP_EXIT
echo.
echo [*] Leaving servers running in background.
ping -n 2 127.0.0.1 >nul
exit /b 0

:KILL_SERVERS
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8501" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq CLIP_AUTO_FastAPI_8765*" /T >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq CLIP_AUTO_Streamlit_8501*" /T >nul 2>&1
exit /b 0
