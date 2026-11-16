<#
.SYNOPSIS
    Automated Windows Setup Script for Stream Clipper (100% Free & Local).
.DESCRIPTION
    Installs:
    - Python 3.12 (via winget if not present)
    - FFmpeg (Gyan.FFmpeg via winget if not in PATH)
    - Virtual Environment (.venv) with faster-whisper, yt-dlp, CUDA/cuDNN DLLs
    - TwitchDownloaderCLI into bin\
    Runs check_setup.py to verify all components.
#>

$ErrorActionPreference = "Stop"
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Stream Clipper - Automated Windows 11 Environment Setup  " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Check / Install FFmpeg
Write-Host "`n[1/5] Checking FFmpeg..." -ForegroundColor Yellow
$ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpegCmd) {
    # Check if newly installed in Winget packages
    $wingetGyan = Get-ChildItem -Path "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Filter "ffmpeg.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($wingetGyan) {
        $binDir = $wingetGyan.DirectoryName
        $env:Path = "$binDir;" + $env:Path
        Write-Host "  Found FFmpeg in Winget cache: $binDir" -ForegroundColor Green
    } else {
        Write-Host "  FFmpeg not found. Installing Gyan.FFmpeg via winget..." -ForegroundColor Yellow
        winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        # Reload PATH from registry
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    }
} else {
    Write-Host "  FFmpeg is installed: $($ffmpegCmd.Source)" -ForegroundColor Green
}

# 2. Check / Install Python 3.12
Write-Host "`n[2/5] Checking Python 3.11/3.12..." -ForegroundColor Yellow
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
$hasPy312 = $false
if ($pyLauncher) {
    $pyCheck = & py -3.12 --version 2>&1
    if ($pyCheck -like "*Python 3.12*") {
        $hasPy312 = $true
        Write-Host "  Python 3.12 found via py launcher: $pyCheck" -ForegroundColor Green
    }
}

if (-not $hasPy312) {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        $ver = & python --version 2>&1
        if ($ver -like "*Python 3.12*" -or $ver -like "*Python 3.11*") {
            $hasPy312 = $true
            Write-Host "  Compatible Python found: $ver" -ForegroundColor Green
        }
    }
}

if (-not $hasPy312) {
    Write-Host "  Installing Python 3.12 via winget..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
}

# 3. Virtual Environment (.venv)
Write-Host "`n[3/5] Setting up Python Virtual Environment (.venv)..." -ForegroundColor Yellow
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "  Creating virtual environment with Python 3.12..." -ForegroundColor Gray
    if ($pyLauncher) {
        & py -3.12 -m venv .venv
    } else {
        & python -m venv .venv
    }
}
Write-Host "  Virtual environment ready: .venv\Scripts\python.exe" -ForegroundColor Green

Write-Host "  Installing dependencies from requirements.txt..." -ForegroundColor Gray
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Write-Host "  Python packages installed successfully." -ForegroundColor Green

# 4. TwitchDownloaderCLI
Write-Host "`n[4/5] Checking TwitchDownloaderCLI..." -ForegroundColor Yellow
$binDir = Join-Path $PSScriptRoot "bin"
if (-not (Test-Path $binDir)) {
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
}

$tdCliPath = Join-Path $binDir "TwitchDownloaderCLI.exe"
if (-not (Test-Path $tdCliPath)) {
    Write-Host "  TwitchDownloaderCLI not found. Downloading latest release..." -ForegroundColor Yellow
    $zipPath = Join-Path $binDir "TwitchDownloaderCLI.zip"
    $downloadUrl = "https://github.com/lay295/TwitchDownloader/releases/download/1.56.5/TwitchDownloaderCLI-1.56.5-Windows-x64.zip"
    Write-Host "  Downloading from $downloadUrl ..." -ForegroundColor Gray
    curl.exe -L -o $zipPath $downloadUrl
    tar.exe -xf $zipPath -C $binDir
    Remove-Item $zipPath -Force
    Write-Host "  Placed TwitchDownloaderCLI at: $tdCliPath" -ForegroundColor Green
} else {
    Write-Host "  TwitchDownloaderCLI is present at: $tdCliPath" -ForegroundColor Green
}

# 5. Run check_setup.py
Write-Host "`n[5/5] Running check_setup.py verification..." -ForegroundColor Yellow
& .\.venv\Scripts\python.exe check_setup.py

Write-Host "`n==========================================================" -ForegroundColor Cyan
Write-Host "  Setup process complete. See results above!              " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
