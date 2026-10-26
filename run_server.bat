@echo off
title ANPR Camera Server - Auto Restart Watchdog
color 0A

:: Ep CUDA Driver JIT bien dich kernel tu dong cho cac dong GPU the he moi (RTX 50 series Blackwell)
set CUDA_FORCE_PTX_JIT=1

:: Triet tieu hoan toan log bao do va canh bao rac bad cseq cua FFmpeg / OpenCV tren Command Prompt
set OPENCV_FFMPEG_LOGLEVEL=-8
set OPENCV_LOG_LEVEL=ERROR
set AV_LOG_FORCE_NOCOLOR=1
set OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp|buffer_size;2048000|max_delay;500000|reorder_queue_size;2048|stimeout;5000000|timeout;5000000

:loop
echo ========================================================
echo [%date% %time%] Khoi chay he thong ANPR Camera Server...
echo ========================================================
python main.py

echo.
echo ========================================================
echo [CANH BAO] Tien trinh Camera bi tat hoac thoat luc: %date% %time% (Exit code: %errorlevel%)
echo He thong se tu dong khoi dong lai sau 5 giay...
echo ========================================================
timeout /t 5 /nobreak
goto loop
