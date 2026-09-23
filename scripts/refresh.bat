@echo off
REM 一键刷新跑步看板数据：重新拉取 Garmin 数据并重新分析。
REM 必须用已装好 garminconnect 的隔离 venv python（系统 python 里没装，用错会导致同步失败）。
REM sync.py 内部会在拉取完成后自动调用 analyze.py，所以这里只跑一个脚本。
set VENV_PY=C:\Users\Fan\.workbuddy\binaries\python\envs\garmin-cn\Scripts\python.exe
if not exist "%VENV_PY%" (
  echo [错误] 找不到 venv python: %VENV_PY%
  echo 请按 requirements.txt 重新创建环境。
  pause
  exit /b 1
)
"%VENV_PY%" "%~dp0sync.py"
if %ERRORLEVEL% NEQ 0 (
  echo.
  echo [警告] 同步未完全成功。原始数据已保持为上一份有效版本（不会被错误数据覆盖），
  echo        请看上面的 [warn] / [fail] 行确认原因。
)
echo.
echo 完成后请重新打开 index.html 查看最新数据。
pause
