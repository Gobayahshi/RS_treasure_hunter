@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ============================================
echo  RS-Treasure Hunter - 주소 좌표 변환 (이어서)
echo ============================================
echo  이미 변환된 판매점은 건너뛰고, 남은 곳만 처리합니다.
echo  창을 닫으면 중단됩니다. 다시 실행하면 이어서 진행됩니다.
echo.
python run_geocode.py
echo.
echo 작업이 끝났습니다. 창을 닫으세요.
pause
