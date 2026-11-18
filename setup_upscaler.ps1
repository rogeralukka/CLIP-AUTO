# Installs Real-ESRGAN (ncnn-vulkan build) into bin\realesrgan so CLIP AUTO can
# AI-sharpen small facecam crops. Free and open source (BSD-3): https://github.com/xinntao/Real-ESRGAN
# Run once from the CLIP AUTO folder:  powershell -ExecutionPolicy Bypass -File .\setup_upscaler.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$url  = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip"
$dest = Join-Path $PSScriptRoot "bin\realesrgan"
$zip  = Join-Path $env:TEMP "realesrgan-ncnn-vulkan-windows.zip"

if (Test-Path (Join-Path $dest "realesrgan-ncnn-vulkan.exe")) {
    Write-Host "Real-ESRGAN is already installed in $dest" -ForegroundColor Green
    exit 0
}

Write-Host "Downloading Real-ESRGAN (about 45 MB) from the official GitHub release..."
curl.exe -L --fail -o $zip $url
if (-not (Test-Path $zip)) { throw "Download failed" }

New-Item -ItemType Directory -Force -Path $dest | Out-Null
Expand-Archive -Path $zip -DestinationPath $dest -Force
Remove-Item $zip -Force

# Some zips put everything inside a sub-folder: move it up
$exe = Get-ChildItem -Path $dest -Recurse -Filter "realesrgan-ncnn-vulkan.exe" | Select-Object -First 1
if (-not $exe) { throw "realesrgan-ncnn-vulkan.exe not found after unzip" }
if ($exe.DirectoryName -ne $dest) {
    Get-ChildItem -Path $exe.DirectoryName | Move-Item -Destination $dest -Force
}

Write-Host "Testing it on the sample image..."
& (Join-Path $dest "realesrgan-ncnn-vulkan.exe") -i (Join-Path $dest "input.jpg") -o (Join-Path $dest "test_out.png") -n realesrgan-x4plus
if (Test-Path (Join-Path $dest "test_out.png")) {
    Remove-Item (Join-Path $dest "test_out.png") -Force
    Write-Host "Real-ESRGAN works. Small facecams in Split / two-cam clips will now be AI-sharpened." -ForegroundColor Green
} else {
    Write-Host "Real-ESRGAN did not produce an image. Update your NVIDIA driver and try again." -ForegroundColor Yellow
}
