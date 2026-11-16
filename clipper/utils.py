"""Utility functions for stream clipper: environment setup, DLL registration, binary path detection."""
import os
import shutil
import sys


_CUDA_INITIALIZED = False


def setup_cuda_dlls():
    """Register NVIDIA CUDA/cuBLAS/cuDNN runtime DLL directories on Windows.
    
    This is required on Windows for faster-whisper / ctranslate2 to find
    cublas64_12.dll and cudnn64_9.dll installed via pip.
    """
    global _CUDA_INITIALIZED
    if _CUDA_INITIALIZED or sys.platform != "win32":
        return
    _CUDA_INITIALIZED = True

    import site
    candidate_paths = []

    # Check site-packages from sys.path and site
    all_site_dirs = []
    try:
        all_site_dirs.extend(site.getsitepackages())
    except Exception:
        pass
    all_site_dirs.extend([p for p in sys.path if "site-packages" in p])

    for site_dir in set(all_site_dirs):
        nvidia_root = os.path.join(site_dir, "nvidia")
        if os.path.isdir(nvidia_root):
            try:
                for sub in os.listdir(nvidia_root):
                    bin_dir = os.path.join(nvidia_root, sub, "bin")
                    if os.path.isdir(bin_dir):
                        candidate_paths.append(bin_dir)
            except Exception:
                pass

    for d in candidate_paths:
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(d)
            except Exception:
                pass
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def get_ffmpeg_path():
    """Find ffmpeg binary in PATH or known winget / local Gyan install locations."""
    # First check standard PATH
    found = shutil.which("ffmpeg")
    if found:
        return found

    # Refresh PATH from system environment in case it was installed recently
    if sys.platform == "win32":
        user_path = os.environ.get("PATH", "")
        # Common Gyan.FFmpeg winget location
        local_app = os.environ.get("LOCALAPPDATA", "")
        gyan_pattern = os.path.join(local_app, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(gyan_pattern):
            for root, _, files in os.walk(gyan_pattern):
                if "ffmpeg.exe" in files:
                    found_path = os.path.join(root, "ffmpeg.exe")
                    bin_dir = os.path.dirname(found_path)
                    os.environ["PATH"] = bin_dir + os.pathsep + user_path
                    return found_path

    return None


def get_twitch_downloader_path():
    """Find TwitchDownloaderCLI executable in bin/ or in PATH."""
    # 1. Project bin folder
    current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local_bin = os.path.join(current_dir, "bin", "TwitchDownloaderCLI.exe")
    if os.path.isfile(local_bin):
        return local_bin

    local_bin_linux = os.path.join(current_dir, "bin", "TwitchDownloaderCLI")
    if os.path.isfile(local_bin_linux):
        return local_bin_linux

    # 2. In PATH
    in_path = shutil.which("TwitchDownloaderCLI") or shutil.which("TwitchDownloaderCLI.exe")
    if in_path:
        return in_path

    return None
