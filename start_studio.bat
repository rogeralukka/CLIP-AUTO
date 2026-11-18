@echo off
cd /d "%~dp0"
rem YouTube changes often; an old yt-dlp only gets the 360p file. Keep it current (quick, skips if up to date).
echo Checking yt-dlp is up to date...
.\.venv\Scripts\python.exe -m pip install -q -U "yt-dlp[default]" >nul 2>&1
start "" http://127.0.0.1:8765
.\.venv\Scripts\python.exe server.py
