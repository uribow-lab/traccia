"""サブコマンドの入口。

    python -m traccia                 → edit（引数なしはエディタ）
    python -m traccia edit [...]
    python -m traccia transcribe [...]
    python -m traccia wfp [...]
    python -m traccia diag [...]

transcribe 側の引数は traccia/transcribe.py のパーサがそのまま受け取る。
既存のコマンド（--diarize --speakers 4 --split-speakers など）を変えないため、
ここでは解釈せずサブコマンド名だけ取り除いて渡している。
"""

from __future__ import annotations

import sys

USAGE = """Traccia — 話者別字幕ツール

  python -m traccia edit [素材フォルダ] [--port N] [--no-browser]
      エディタをブラウザで開く（既定の素材フォルダ: ./resources）

  python -m traccia transcribe 動画.mp4 [--diarize --speakers 4 --split-speakers ...]
      文字起こしと話者分離を行い .srt / .txt を出力する
      詳しくは  python -m traccia transcribe --help

  python -m traccia wfp プロジェクト.wfp [--per-speaker --speakers 名前,名前]
      Filmora で直し終えたプロジェクトから最終版の .srt を取り出す
      既定の出力先: <.wfp と同じ場所>/export/<名前>_wfp.srt

  python -m traccia diag セットフォルダ [--chunk 240] [--no-run] [--yes]
      文字起こしの時刻のずれを測る（生の時刻を残して確定版と突き合わせる）
      edit.json には書かない。--no-run なら課金なしで測り直すだけ

  ダブルクリックで開くなら Mac は Traccia.command、Windows は Traccia.bat
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "edit"

    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if cmd == "transcribe":
        # 重い依存（torch / pyannote / faster-whisper）はここに入ってから読み込む
        from . import transcribe
        sys.argv = [f"{sys.argv[0]} transcribe"] + argv[1:]
        return transcribe.main()

    if cmd == "wfp":
        from .wfp import main as wfp_main
        return wfp_main(argv[1:])

    if cmd == "diag":
        from .diag_cli import main as diag_main
        return diag_main(argv[1:])

    if cmd == "edit":
        argv = argv[1:]
    elif cmd.startswith("-") or not argv:
        pass                      # 引数なし、またはオプションだけなら edit 扱い
    else:
        print(f"知らないサブコマンドです: {cmd}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    from .editor.cli import main as edit_main
    return edit_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
