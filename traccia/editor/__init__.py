"""Traccia エディタ（案A: 3ペイン構成）。

起動:
    .venv/bin/python -m traccia edit              # ./resources を見る
    .venv/bin/python -m traccia edit ~/素材 --port 8791

Mac は Traccia.command、Windows は Traccia.bat をダブルクリックでも開く。
"""

# 版はパッケージ側の 1 か所だけで持つ。ここは読み替えるだけにして、
# リリースのたびに 2 か所直す（そして片方を忘れる）のを避ける。
from .. import __version__

__all__ = ["__version__"]
