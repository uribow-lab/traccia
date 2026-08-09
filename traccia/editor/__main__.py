"""python -m traccia.editor でも直接起動できるようにしておく。

通常の入口は python -m traccia edit（traccia/__main__.py）。
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
