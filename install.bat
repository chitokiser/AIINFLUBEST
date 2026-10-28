@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  echo [1/3] 가상환경 만드는 중...
  python -m venv .venv || goto :error
)
echo [2/3] 패키지 설치 중...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :error
if not exist .env copy .env.example .env >nul
echo [3/3] 테스트 실행 중...
.venv\Scripts\python.exe -m pytest -q || goto :error
echo.
echo 설치 완료. run_web.bat 으로 대시보드를 여세요.
pause
exit /b 0
:error
echo 설치 중 오류가 발생했습니다.
pause
exit /b 1
