"""
Helper module to synchronize environment variables directly into the C Runtime (MSVCRT / UCRTBASE)
and suppress all FFmpeg / OpenCV red error banners and jitter warnings in the Windows console.
"""
import os
import sys
import ctypes

# 1. Python Process Environment
# Chu y: Trong FFmpeg RTSP demuxer, timeout cho client la stimeout (microseconds).
# TUYET DOI KHONG dung 'timeout;' vi trong ffmpeg rtsp do la co danh cho che do listen (server)!
CAPTURE_OPTS = "rtsp_transport;tcp|buffer_size;2048000|max_delay;500000|reorder_queue_size;2048|stimeout;5000000"

ENV_VARS = {
    "CUDA_FORCE_PTX_JIT": "1",
    "OPENCV_FFMPEG_LOGLEVEL": "-8",  # AV_LOG_QUIET: triet tieu hoan toan log do va canh bao FFmpeg
    "OPENCV_LOG_LEVEL": "SILENT",
    "AV_LOG_FORCE_NOCOLOR": "1",
    "OPENCV_FFMPEG_CAPTURE_OPTIONS": CAPTURE_OPTS,
}

for k, v in ENV_VARS.items():
    os.environ[k] = v

# 2. Windows C Runtime Environment Synchronization (msvcrt.dll, ucrtbase.dll)
# Chu y: opencv_videoio_ffmpeg4130_64.dll duoc build va link voi msvcrt.dll (MinGW).
# Python 3 dung ucrtbase.dll. os.environ cua Python KHONG dong bo sang msvcrt.dll!
# Do do bat buoc phai dung ctypes de goi truc tiep _putenv vao msvcrt.dll va ucrtbase.dll.
if sys.platform == "win32":
    for lib_name in ("msvcrt", "ucrtbase"):
        try:
            cdll = getattr(ctypes.cdll, lib_name, None)
            if not cdll:
                cdll = ctypes.CDLL(lib_name)
            for k, v in ENV_VARS.items():
                env_entry = f"{k}={v}".encode("utf-8")
                if hasattr(cdll, "_putenv"):
                    cdll._putenv(env_entry)
                if hasattr(cdll, "_wputenv"):
                    cdll._wputenv(f"{k}={v}")
        except Exception:
            pass

    # Dong bo Win32 API Kernel32 Process Environment Block
    try:
        kernel32 = ctypes.windll.kernel32
        for k, v in ENV_VARS.items():
            kernel32.SetEnvironmentVariableW(k, v)
    except Exception:
        pass

# 3. OpenCV internal logger suppression
def silence_cv2():
    try:
        import cv2
        if hasattr(cv2, "utils") and hasattr(cv2.utils, "logging"):
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except Exception:
        pass

silence_cv2()
