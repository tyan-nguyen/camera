@echo off
title ANPR Camera Server - Auto Restart Watchdog
color 0A

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
