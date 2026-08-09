@echo off
rem ダブルクリックで Traccia を開く（Windows 用）
chcp 65001 > nul
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
  echo Python 環境が見つかりません: %CD%\%PY%
  echo.
  echo セットアップ:
  echo   py -3.12 -m venv .venv
  echo   .venv\Scripts\python -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

cls
echo Traccia
echo ブラウザが開きます。終わるときは、このウィンドウで Ctrl+C。
echo.

"%PY%" -m traccia edit
if errorlevel 1 pause
