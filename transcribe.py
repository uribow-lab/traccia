#!/usr/bin/env python3
"""互換用の入口。中身は traccia/transcribe.py に移した。

これまでのコマンドがそのまま動くように残してある。

    .venv/bin/python transcribe.py 動画.mp4 --diarize --speakers 4 --split-speakers
    .venv\\Scripts\\python transcribe.py 動画.mp4 --diarize --device cuda

新しい書き方はこちら（どちらでも同じ）。

    .venv/bin/python -m traccia transcribe 動画.mp4 --diarize --speakers 4
"""

import importlib
import sys
from pathlib import Path

# どこから呼ばれても traccia を見つけられるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    sys.exit(importlib.import_module("traccia.transcribe").main())
