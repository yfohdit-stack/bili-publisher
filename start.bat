@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo 没找到 python，请先安装 Python 3.9 以上版本并勾选 Add to PATH
  pause
  exit /b 1
)

if not exist ".venv" (
  echo 首次运行，正在创建虚拟环境...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q --upgrade pip
pip install -q -r requirements.txt

python run.py %*
pause
