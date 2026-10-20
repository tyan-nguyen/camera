@echo off
title ANPR Camera Server - Auto Restart Watchdog
color 0A

:: Ep CUDA Driver JIT bien dich kernel tu dong cho cac dong GPU the he moi (RTX 50 series Blackwell)
set CUDA_FORCE_PTX_JIT=1

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
