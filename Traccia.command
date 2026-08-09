#!/bin/bash
# ダブルクリックで Traccia を開く（Mac 用）
#
# このファイルは Finder からダブルクリックできる。Dock に置いてもよい。
# 終了するときは、開いたターミナルで Ctrl+C を押すか、ウィンドウを閉じる。

cd "$(dirname "$0")" || exit 1

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "Python 環境が見つかりません: $(pwd)/$PY"
  echo
  echo "セットアップ:"
  echo "  python3.12 -m venv .venv"
  echo "  .venv/bin/python -m pip install -r requirements.txt"
  echo
  read -r -p "Enter で閉じます"
  exit 1
fi

clear
echo "Traccia"
echo "ブラウザが開きます。終わるときは、このウィンドウで Ctrl+C。"
echo

"$PY" -m traccia edit --port 8791
status=$?

echo
if [ $status -ne 0 ]; then
  echo "終了コード $status で終わりました。"
  read -r -p "Enter で閉じます"
fi
