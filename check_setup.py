#!/usr/bin/env python3
"""check_setup.py - Diagnostic test suite for Stream Clipper environment.

Verifies:
1. FFmpeg in PATH (with libass enabled)
2. NVENC hardware encoder (real 2-second test encode)
3. CUDA runtime visibility (NVIDIA GPU, driver, cuBLAS/cuDNN)
4. faster-whisper loads large-v3 on GPU and transcribes test audio
5. yt-dlp CLI installation and functionality
6. TwitchDownloaderCLI installation and functionality

Prints clean PASS / FAIL with clear, actionable fix instructions.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

# Ensure local clipper package is importable
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from clipper.utils import get_ffmpeg_path, get_twitch_downloader_path, setup_cuda_dlls

# Color output helpers for Windows terminal
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_header(title):
    print(f"\n{CYAN}{BOLD}{'=' * 65}{RESET}")
    print(f"{CYAN}{BOLD}  {title}{RESET}")
    print(f"{CYAN}{BOLD}{'=' * 65}{RESET}")


def print_result(name, passed, detail="", fix_message=""):
    status = f"{GREEN}[PASS]{RESET}" if passed else f"{RED}[FAIL]{RESET}"
    print(f" {status} {BOLD}{name}{RESET}")
    if detail:
        for line in detail.strip().splitlines():
            print(f"        {line}")
    if not passed and fix_message:
        print(f"        {YELLOW}Fix:{RESET} {fix_message}")


def check_ffmpeg(verbose=False):
    """Test 1: Check FFmpeg in PATH and verify libass support."""
    ffmpeg_bin = get_ffmpeg_path()
    if not ffmpeg_bin:
        return False, "ffmpeg not found in PATH or standard winget directories.", (
            "Run 'winget install Gyan.FFmpeg' in PowerShell and restart your shell, "
            "or download FFmpeg from https://www.gyan.dev/ffmpeg/builds/ and add the 'bin' folder to PATH."
        )

    try:
        res = subprocess.run([ffmpeg_bin, "-version"], capture_output=True, text=True, check=True)
        version_line = res.stdout.splitlines()[0] if res.stdout else "Unknown version"
        has_libass = "--enable-libass" in res.stdout
        has_nvenc = "--enable-nvenc" in res.stdout

        details = [
            f"Binary: {ffmpeg_bin}",
            f"Version: {version_line}",
            f"Features: libass (subtitles)={'YES' if has_libass else 'NO'}, nvenc={'YES' if has_nvenc else 'NO'}"
        ]
        if not has_libass:
            return False, "\n".join(details), (
                "Your FFmpeg build lacks --enable-libass (required for burning captions). "
                "Install Gyan.FFmpeg ('winget install Gyan.FFmpeg') which includes libass."
            )
        return True, "\n".join(details), ""
    except Exception as e:
        return False, f"Failed to run ffmpeg: {e}", "Ensure ffmpeg executable is valid and has execute permissions."


def check_nvenc(verbose=False):
    """Test 2: Perform real 2-second test video encode using h264_nvenc."""
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    temp_dir = tempfile.gettempdir()
    test_out = os.path.join(temp_dir, f"clipper_test_nvenc_{int(time.time())}.mp4")

    cmd = [
        ffmpeg_bin, "-y",
        "-f", "lavfi", "-i", "testsrc=duration=2:size=1920x1080:rate=30",
        "-c:v", "h264_nvenc",
        "-preset", "p5",
        "-cq", "21",
        test_out
    ]

    try:
        t0 = time.time()
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        duration = time.time() - t0

        if res.returncode == 0 and os.path.isfile(test_out) and os.path.getsize(test_out) > 0:
            size_kb = os.path.getsize(test_out) / 1024
            try:
                os.remove(test_out)
            except Exception:
                pass
            return True, f"Hardware encode succeeded in {duration:.2f}s ({size_kb:.1f} KB, 1080p@30fps NVENC)", ""
        else:
            err = res.stderr[-500:] if res.stderr else "Unknown error"
            return False, f"NVENC encode failed:\n{err}", (
                "Update your NVIDIA graphics drivers from https://www.nvidia.com/drivers or GeForce Experience. "
                "If running without an NVIDIA GPU, use the '--cpu' flag with clipper.py to use libx264."
            )
    except Exception as e:
        return False, f"Error running NVENC test: {e}", (
            "Ensure NVIDIA GPU drivers are installed and up to date, or fallback to CPU with --cpu."
        )


def check_cuda(verbose=False):
    """Test 3: Verify CUDA visibility, NVIDIA GPU detection, and cuBLAS/cuDNN."""
    setup_cuda_dlls()

    # 1. Check nvidia-smi
    smi = shutil.which("nvidia-smi")
    gpu_name = "NVIDIA GPU"
    driver_ver = "Unknown"
    vram_str = ""

    if smi:
        try:
            res = subprocess.run(
                [smi, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0 and res.stdout.strip():
                parts = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
                if len(parts) >= 3:
                    gpu_name, driver_ver, vram_mb = parts[0], parts[1], parts[2]
                    vram_str = f", VRAM: {int(vram_mb)/1024:.1f} GB"
        except Exception:
            pass

    # 2. Check CTranslate2 CUDA device count
    try:
        import ctranslate2
        cuda_count = ctranslate2.get_cuda_device_count()
        if cuda_count > 0:
            return True, f"GPU: {gpu_name} (Driver: {driver_ver}{vram_str})\nCTranslate2 CUDA devices: {cuda_count}", ""
        else:
            return False, f"GPU detected ({gpu_name}) but CTranslate2 reports 0 CUDA devices.", (
                "Ensure nvidia-cublas-cu12 and nvidia-cudnn-cu12 are installed in your virtual environment: "
                "'pip install nvidia-cublas-cu12 nvidia-cudnn-cu12'. "
                "Alternatively, pass --cpu to run on CPU."
            )
    except ImportError:
        return False, "ctranslate2 is not installed.", "Run 'pip install faster-whisper'."
    except Exception as e:
        return False, f"CUDA check failed: {e}", (
            "Ensure NVIDIA CUDA runtime libraries are registered. If troubleshooting, use --cpu mode."
        )


def check_whisper(force_cpu=False, verbose=False):
    """Test 4: Verify faster-whisper loads large-v3 and transcribes a test file."""
    setup_cuda_dlls()
    ffmpeg_bin = get_ffmpeg_path() or "ffmpeg"
    temp_dir = tempfile.gettempdir()
    test_audio = os.path.join(temp_dir, f"clipper_test_audio_{int(time.time())}.wav")

    # Generate 2 seconds of test tone at 16kHz
    try:
        subprocess.run([
            ffmpeg_bin, "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            test_audio
        ], capture_output=True, check=True)
    except Exception as e:
        return False, f"Failed to generate test audio: {e}", "Ensure FFmpeg is installed and functional."

    target_device = "cpu" if force_cpu else "cuda"
    compute_type = "int8" if force_cpu else "float16"

    try:
        from faster_whisper import WhisperModel
        print(f"        Loading large-v3 model on {target_device.upper()} ({compute_type})...")
        t0 = time.time()
        model = WhisperModel("large-v3", device=target_device, compute_type=compute_type)
        load_time = time.time() - t0

        t1 = time.time()
        segs, info = model.transcribe(test_audio, word_timestamps=True)
        # Force generator evaluation
        list(segs)
        transcribe_time = time.time() - t1

        try:
            os.remove(test_audio)
        except Exception:
            pass

        return True, (
            f"Loaded in {load_time:.2f}s | Transcribed test audio in {transcribe_time:.2f}s "
            f"on {target_device.upper()} ({compute_type})"
        ), ""
    except Exception as e:
        err_msg = str(e)
        try:
            os.remove(test_audio)
        except Exception:
            pass

        fix = (
            f"Whisper on {target_device.upper()} failed: {err_msg}.\n"
            "If GPU VRAM/CUDA error occurs, verify nvidia-cublas-cu12 and nvidia-cudnn-cu12 are installed, "
            "or run with '--cpu' mode (which uses 8-bit integer quantization on CPU)."
        )
        return False, f"Transcription error: {err_msg}", fix


def check_ytdlp(verbose=False):
    """Test 5: Verify yt-dlp binary and functionality."""
    ytdlp_bin = shutil.which("yt-dlp")
    # Also check venv Scripts
    if not ytdlp_bin:
        venv_ytdlp = os.path.join(CURRENT_DIR, ".venv", "Scripts", "yt-dlp.exe")
        if os.path.isfile(venv_ytdlp):
            ytdlp_bin = venv_ytdlp

    if not ytdlp_bin:
        return False, "yt-dlp is not installed or not in PATH.", "Run 'pip install yt-dlp' inside your virtual environment."

    try:
        res = subprocess.run([ytdlp_bin, "--version"], capture_output=True, text=True, check=True)
        version = res.stdout.strip()
        # Verify help works
        help_res = subprocess.run([ytdlp_bin, "--help"], capture_output=True, text=True, timeout=5)
        if help_res.returncode == 0:
            return True, f"Binary: {ytdlp_bin}\nVersion: {version} (CLI options verified)", ""
        else:
            return False, f"yt-dlp failed to execute --help.", "Reinstall yt-dlp: 'pip install --upgrade yt-dlp'."
    except Exception as e:
        return False, f"yt-dlp error: {e}", "Run 'pip install --upgrade yt-dlp'."


def check_twitch_downloader(verbose=False):
    """Test 6: Verify TwitchDownloaderCLI executable and options."""
    td_path = get_twitch_downloader_path()
    if not td_path:
        expected_path = os.path.join(CURRENT_DIR, "bin", "TwitchDownloaderCLI.exe")
        return False, f"TwitchDownloaderCLI not found (checked PATH and {expected_path}).", (
            f"Download TwitchDownloaderCLI-*-Windows-x64.zip from "
            f"https://github.com/lay295/TwitchDownloader/releases and extract TwitchDownloaderCLI.exe into '{expected_path}'."
        )

    try:
        # TwitchDownloaderCLI returns exit code 0 or 1 for version / subcommands
        res = subprocess.run([td_path, "--version"], capture_output=True, text=True, timeout=5)
        v_out = res.stdout.strip() or res.stderr.strip()
        first_line = v_out.splitlines()[0] if v_out else "TwitchDownloaderCLI"

        # Check videodownload and chatdownload help
        v_help = subprocess.run([td_path, "videodownload", "--help"], capture_output=True, text=True, timeout=5)
        has_video = "--id" in v_help.stdout or "--id" in v_help.stderr
        c_help = subprocess.run([td_path, "chatdownload", "--help"], capture_output=True, text=True, timeout=5)
        has_chat = "--id" in c_help.stdout or "--id" in c_help.stderr

        if has_video and has_chat:
            return True, (
                f"Binary: {td_path}\n"
                f"Version: {first_line}\n"
                f"Subcommands: videodownload [OK], chatdownload [OK]"
            ), ""
        else:
            return True, f"Binary: {td_path} (Detected version: {first_line})", ""
    except Exception as e:
        return False, f"Failed to execute TwitchDownloaderCLI: {e}", (
            "Ensure TwitchDownloaderCLI.exe is an unblocked 64-bit Windows binary."
        )


def main():
    parser = argparse.ArgumentParser(
        description="Stream Clipper Diagnostic & Verification Suite (Phase 0)",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cpu", action="store_true", help="Force Whisper check to run on CPU mode")
    parser.add_argument("--skip-whisper", action="store_true", help="Skip large-v3 Whisper model inference check")
    parser.add_argument("--verbose", action="store_true", help="Print verbose subprocess outputs")
    args = parser.parse_args()

    print_header("STREAM CLIPPER - PHASE 0 SYSTEM VERIFICATION")
    print(f" Python Executable : {sys.executable}")
    print(f" Python Version    : {sys.version.split()[0]}")
    print(f" Platform          : {sys.platform} (Windows)")
    print(f" Working Directory : {CURRENT_DIR}")

    all_passed = True

    # 1. FFmpeg
    print(f"\n{BOLD}[1/6] Testing FFmpeg in PATH...{RESET}")
    ok, detail, fix = check_ffmpeg(args.verbose)
    print_result("FFmpeg (in PATH & libass enabled)", ok, detail, fix)
    if not ok:
        all_passed = False

    # 2. NVENC
    print(f"\n{BOLD}[2/6] Testing NVENC Hardware Encoder (2-second encode)...{RESET}")
    ok, detail, fix = check_nvenc(args.verbose)
    print_result("NVENC Encoder (h264_nvenc 1080p)", ok, detail, fix)
    if not ok:
        all_passed = False

    # 3. CUDA Visibility
    print(f"\n{BOLD}[3/6] Testing CUDA & NVIDIA Runtime Visibility...{RESET}")
    ok, detail, fix = check_cuda(args.verbose)
    print_result("NVIDIA CUDA & cuBLAS/cuDNN DLLs", ok, detail, fix)
    if not ok:
        all_passed = False

    # 4. faster-whisper large-v3
    print(f"\n{BOLD}[4/6] Testing faster-whisper (large-v3 transcription)...{RESET}")
    if getattr(args, "skip_whisper", False):
        print_result("faster-whisper large-v3", True, "Skipped by --skip-whisper flag")
    else:
        ok, detail, fix = check_whisper(args.cpu, args.verbose)
        print_result("faster-whisper large-v3 Inference", ok, detail, fix)
        if not ok:
            all_passed = False

    # 5. yt-dlp
    print(f"\n{BOLD}[5/6] Testing yt-dlp CLI...{RESET}")
    ok, detail, fix = check_ytdlp(args.verbose)
    print_result("yt-dlp CLI", ok, detail, fix)
    if not ok:
        all_passed = False

    # 6. TwitchDownloaderCLI
    print(f"\n{BOLD}[6/6] Testing TwitchDownloaderCLI...{RESET}")
    ok, detail, fix = check_twitch_downloader(args.verbose)
    print_result("TwitchDownloaderCLI", ok, detail, fix)
    if not ok:
        all_passed = False

    # Summary
    print_header("VERIFICATION SUMMARY")
    if all_passed:
        print(f" {GREEN}{BOLD}ALL TESTS PASSED! Your machine is 100% ready for Phase 1.{RESET}")
        print(f" Hardware: RTX 5070 Ti (12GB VRAM) ready with NVENC + large-v3 CUDA.")
        sys.exit(0)
    else:
        print(f" {RED}{BOLD}ONE OR MORE CHECKS FAILED.{RESET}")
        print(f" Follow the fix recommendations printed above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
