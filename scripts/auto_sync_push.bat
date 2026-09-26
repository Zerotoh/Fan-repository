@echo off
REM 一键：本地同步 Garmin 数据 + 分析，并推送到 GitHub（触发 Pages 自动部署）。
REM 供每日自动化调用；非交互、无 pause。
cd /d "%~dp0.."
set "PATH=C:\Users\Fan\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd;%PATH%"

REM 1) 同步 + 分析（必须用已装 garminconnect 的隔离 venv python）
set "VENV_PY=C:\Users\Fan\.workbuddy\binaries\python\envs\garmin-cn\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo [error] 找不到 venv python: %VENV_PY%
  exit /b 1
)
echo [1/2] 同步 Garmin 并分析...
"%VENV_PY%" "%~dp0sync.py"
if %ERRORLEVEL% NEQ 0 (
  echo [warn] 同步失败，保留上一份有效数据，跳过推送。
  exit /b 1
)

REM 1.5) 把最新看板刷进 dist/ 发布包（dist 是 gitignore 的本地发布产物，
REM       WorkBuddy 在线发布只认 dist/；这里先同步好，下次发布即最新）
if not exist "dist\data\processed" mkdir "dist\data\processed"
copy /Y "index.html" "dist\index.html" >nul
copy /Y "data\processed\dashboard_data.js" "dist\data\processed\dashboard_data.js" >nul
echo [1.5] 已刷新 dist/ 发布包

REM 2) 提交并推送（用仓库内 pat.txt 的 token，用完即还原 remote）
echo [2/2] 提交并推送...
set "TOKEN="
if exist "pat.txt" set /p TOKEN=<"pat.txt"
set "TOKEN=%TOKEN: =%"
if "%TOKEN%"=="" (
  echo [error] 未找到 pat.txt，无法推送。
  exit /b 1
)
git remote set-url origin https://%TOKEN%@github.com/Zerotoh/Fan-repository.git
git config user.name >nul 2>&1 || git config user.name "Zerotoh"
git config user.email >nul 2>&1 || git config user.email "Zerotoh@users.noreply.github.com"
git add index.html data/processed data/plan_config.json .nojekyll
git commit -m "data: daily sync %date:~0,4%-%date:~5,2%-%date:~6,2%" || echo "    (no local changes to commit)"
git pull --rebase --autostash origin main
git push -u origin main
git remote set-url origin https://github.com/Zerotoh/Fan-repository.git
echo Done.
