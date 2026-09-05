@echo off
rem 動画ファイルをこの .bat にドラッグ＆ドロップすると、GPU で文字起こしする（Windows 用）
rem
rem HuggingFace のトークンは環境変数 HF_TOKEN から読む。
rem   setx HF_TOKEN hf_xxxxxxxx     ← 一度だけ実行しておく（新しいウィンドウから有効）
chcp 65001 > nul
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
  echo Python 環境が見つかりません: %CD%\%PY%
  pause
  exit /b 1
)

if "%~1"=="" (
  echo 使い方: 動画ファイルを、この Traccia 文字起こし.bat にドラッグ＆ドロップしてください。
  echo.
  pause
  exit /b 1
)

if "%HF_TOKEN%"=="" (
  echo 話者分離には HuggingFace のトークンが必要です。
  echo 次のコマンドを一度だけ実行してから、新しいウィンドウでやり直してください。
  echo.
  echo   setx HF_TOKEN hf_xxxxxxxx
  echo.
  pause
  exit /b 1
)

set /p SPEAKERS=話者は何人ですか（分かれば数字、不明なら空のまま Enter）:

rem 計算精度は指定しない。--compute-type の既定 auto が、GPU なら float32、
rem CPU なら int8 を選ぶ。GPU の int8 は出力が壊れることがある（TRAC-27）。
set OPT=--diarize --split-speakers --device cuda
if not "%SPEAKERS%"=="" set OPT=%OPT% --speakers %SPEAKERS%

echo.
echo 文字起こしを始めます: %~nx1
echo   %OPT%
echo.

"%PY%" -m traccia transcribe "%~1" %OPT% --hf-token %HF_TOKEN%

echo.
echo 出力先: %~dp1dest\%~n1\
echo このフォルダごと、Mac の resources/ に置いてください。
echo （動画も同じフォルダに入れると、そのまま 1 セットになります）
pause
