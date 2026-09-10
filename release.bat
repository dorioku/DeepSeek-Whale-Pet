@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ============================================================
rem  DeepSeek 小鲸鱼桌宠 —— 一键构建 / 发布
rem
rem  用法：
rem    release.bat            仅构建，产物 dist\WhalePet.exe
rem    release.bat 0.3        构建 + 提交 + 打标签 + 推送 + 发布 GitHub Release v0.3
rem
rem  前置：Python 3.10+、git；发布还需 GitHub CLI(gh) 且已 gh auth login。
rem  依赖由脚本自动安装（requirements.txt / pyinstaller / pillow）。
rem  打包参数在 WhalePet.spec 中修改。
rem ============================================================

set "VER=%~1"
if defined VER set "VER=%VER:v=%"
set "TAG=v%VER%"

rem ---- 访问 GitHub 的代理；本机不需要代理时注释掉下面两行 ----
if not defined HTTPS_PROXY set "HTTPS_PROXY=socks5://127.0.0.1:10808"
set "HTTP_PROXY=%HTTPS_PROXY%"

if defined VER (
  echo %TAG% | findstr /r "^v[0-9][0-9]*\.[0-9][0-9.]*$" >nul
  if errorlevel 1 (
    echo [错误] 版本号格式不对，应形如：release.bat 0.3
    exit /b 1
  )
)

echo.
echo === [1/5] 检查 Python ===
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请先安装 Python 3.10+
  exit /b 1
)

echo === [2/5] 安装构建依赖 ===
python -m pip install -r requirements.txt pyinstaller pillow -q
if errorlevel 1 (
  echo [错误] 依赖安装失败
  exit /b 1
)

echo === [3/5] PyInstaller 打包（单文件） ===
python -m PyInstaller --noconfirm --clean WhalePet.spec
if errorlevel 1 (
  echo [错误] 打包失败，请检查上方输出
  exit /b 1
)

set "EXE=dist\WhalePet.exe"
if not exist "%EXE%" (
  echo [错误] 未找到产物 %EXE%
  exit /b 1
)

set /a SIZEMB=0
for %%A in ("%EXE%") do set /a SIZEMB=%%~zA/1048576

set "SHA="
for /f "skip=1 delims=" %%H in ('certutil -hashfile "%EXE%" SHA256') do (
  if not defined SHA set "SHA=%%H"
)
set "SHA=%SHA: =%"

echo.
echo    产物  : %EXE% ^(约 %SIZEMB% MB^)
echo    SHA256: %SHA%
echo.

if not defined VER (
  echo [完成] 仅构建完成，未发布。发布请运行：release.bat 0.3
  goto :done
)

echo === [4/5] 准备 %TAG% ===
git rev-parse --git-dir >nul 2>nul
if errorlevel 1 (
  echo [错误] 当前目录不是 git 仓库
  exit /b 1
)

git rev-parse -q --verify "refs/tags/%TAG%" >nul 2>nul
if not errorlevel 1 (
  echo [错误] 标签 %TAG% 已存在，请换一个版本号
  exit /b 1
)

gh auth status >nul 2>nul
if errorlevel 1 (
  echo [错误] GitHub CLI 未登录，请先运行：gh auth login
  exit /b 1
)

set "REPO="
for /f "delims=" %%R in ('gh repo view --json nameWithOwner --jq .nameWithOwner 2^>nul') do set "REPO=%%R"
if not defined REPO set "REPO=dorioku/DeepSeek-Whale-Pet"

set "DIRTY="
for /f "delims=" %%L in ('git status --porcelain') do set "DIRTY=1"
if defined DIRTY (
  echo    待提交改动：
  git status --short
)

echo.
set /p "GO=即将发布 %TAG% 到 %REPO%（已推送/发布后不可撤销），确认？(y/N) "
if /i not "!GO!"=="y" (
  echo 已取消，未做任何改动。
  exit /b 1
)

if defined DIRTY (
  git add -A
  git commit -m "chore(release): %TAG%" >nul
  if errorlevel 1 (
    echo [错误] 提交失败
    exit /b 1
  )
)

git tag -a "%TAG%" -m "%TAG%"
if errorlevel 1 (
  echo [错误] 创建标签失败
  exit /b 1
)

git push origin HEAD
if errorlevel 1 (
  echo [错误] 推送分支失败（标签已创建在本地，修正后可重跑）
  exit /b 1
)
git push origin "%TAG%"
if errorlevel 1 (
  echo [错误] 推送标签失败
  exit /b 1
)

echo === [5/5] 创建 GitHub Release ===
set "NOTES=%TEMP%\whalepet-%TAG%-notes.md"
(
  echo **DeepSeek 小鲸鱼桌宠 · 独立桌面版** —— %TAG%
  echo.
  echo Windows 右下角的透明置顶小鲸鱼：直连 DeepSeek API 显示余额，并统计「今日已用」与「每轮对话消耗」。
  echo.
  echo ## 下载
  echo.
  echo ^| 文件 ^| 说明 ^|
  echo ^| --- ^| --- ^|
  echo ^| WhalePet.exe ^| Windows 10 / 11 x64 单文件版（约 %SIZEMB% MB），双击即可运行，无需安装 Python ^|
  echo.
  echo 首次运行会弹出设置窗口，填入 **DeepSeek API Key** 后自动缓存到 `%%APPDATA%%\WhalePet\config.json`；
  echo 把 `config.json` 放在 exe 同目录即可作为便携版使用。
  echo.
  echo ## 校验值
  echo.
  echo ```
  echo SHA256  %SHA%
  echo ```
  echo.
  echo ## 说明
  echo.
  echo - 未做代码签名，Windows SmartScreen 可能提示「未知发布者」，选择「仍要运行」即可。
  echo - 由 `release.bat` 构建发布（PyInstaller 单文件打包，配置见 `WhalePet.spec`）。
  echo - 源码与更新日志：https://github.com/%REPO%
  echo - 美术素材、峰谷定价规则与气泡视觉参数来自 [DeepSeek-Balance-Whale-Widget](https://github.com/MeteorNOX/DeepSeek-Balance-Whale-Widget)（MIT License），详见仓库 README 的「致谢」。
) > "%NOTES%"

gh release create "%TAG%" "%EXE%" --title "%TAG%" --notes-file "%NOTES%" --verify-tag
if errorlevel 1 (
  echo [错误] 创建 Release 失败；标签已推送，修正后可重跑：gh release create %TAG% "%EXE%" --notes-file "%NOTES%"
  exit /b 1
)
del "%NOTES%" >nul 2>nul

echo.
echo [完成] https://github.com/%REPO%/releases/tag/%TAG%

:done
rem 结尾等待按键（自动化场景可先 set WHALEPET_NO_PAUSE=1 跳过）
if not defined WHALEPET_NO_PAUSE pause
endlocal
