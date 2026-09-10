@echo off
rem ============================================================
rem  DeepSeek 小鲸鱼桌宠 —— 打包为单文件 exe
rem  依赖：python 3.10+，先 pip install -r requirements.txt pyinstaller pillow
rem  （pillow 用于把 assets\DSniang1.png 自动转成 exe 图标）
rem ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请先安装 Python 3.10+
  pause
  exit /b 1
)

python -m pip install -r requirements.txt pyinstaller pillow -q
if errorlevel 1 (
  echo [错误] 依赖安装失败
  pause
  exit /b 1
)

python -m PyInstaller --noconfirm --clean ^
  --name WhalePet ^
  --onefile ^
  --windowed ^
  --icon assets\DSniang1.png ^
  --add-data "assets;assets" ^
  --hidden-import PySide6.QtSvg ^
  --hidden-import PySide6.QtMultimedia ^
  main.py

if errorlevel 1 (
  echo [错误] 打包失败，请检查上方输出
  pause
  exit /b 1
)

echo.
echo [完成] 产物：dist\WhalePet.exe
echo 首次运行会弹出设置窗口，填入 DeepSeek API Key 即可。
pause
