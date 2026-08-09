"""Traccia エディタの起動処理。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .server import serve

DEFAULT_PORT = 8791   # 8770 は macOS の sharingd が握っていることがあるので避ける


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m traccia edit",
        description="Traccia エディタ（ローカルサーバー + ブラウザ）",
    )
    ap.add_argument(
        "resources", nargs="?", default=None,
        help="セットフォルダの親ディレクトリ（既定: ./resources）",
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"待受ポート（既定: {DEFAULT_PORT}）")
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    args = ap.parse_args(argv)

    root = Path(args.resources) if args.resources else Path.cwd() / "resources"
    root = root.expanduser().resolve()

    if not root.is_dir():
        print(f"素材フォルダが見つかりません: {root}", file=sys.stderr)
        print("  1 セット = 1 フォルダで、動画と .srt を入れて置いてください。", file=sys.stderr)
        print("  別の場所を使うなら: python -m traccia edit /path/to/素材", file=sys.stderr)
        return 1

    serve(root, port=args.port, open_browser=not args.no_browser)
    return 0
