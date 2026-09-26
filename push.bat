@echo off
rem Sync local repo with GitHub then push.
rem Handles the daily auto-commit that GitHub Actions makes on the remote.
rem Non-interactive: the PAT is put into the remote URL for this run only.
cd /d "%~dp0"
set "PATH=C:\Users\Fan\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd;%PATH%"

rem 优先从本地 pat.txt 读取 token（该文件已 gitignore，请勿提交到仓库）
set "TOKEN="
if exist "pat.txt" set /p TOKEN=<"pat.txt"
set "TOKEN=%TOKEN: =%"
if "%TOKEN%"=="" set /p TOKEN="Paste your GitHub PAT (repo + workflow scopes): "

git remote set-url origin https://%TOKEN%@github.com/Zerotoh/Fan-repository.git

rem Ensure a git identity exists (required to create commits).
git config user.name >nul 2>&1 || git config user.name "Zerotoh"
git config user.email >nul 2>&1 || git config user.email "Zerotoh@users.noreply.github.com"

echo.
echo [1/3] Commit local changes (if any)...
git add index.html data/processed data/plan_config.json .nojekyll
git commit -m "data: dashboard sync update" || echo "    (no local changes to commit)"

echo.
echo [2/3] Integrate remote commits (daily auto-sync) via rebase...
git pull --rebase --autostash origin main

echo.
echo [3/3] Push...
git push -u origin main

git remote set-url origin https://github.com/Zerotoh/Fan-repository.git
echo.
echo Done. Remote URL reset to the token-free form.
pause
