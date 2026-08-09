"""Traccia — 話者別字幕ツール。

名前はイタリア語の traccia（なぞった跡 / 音声トラック）から。
音声を文字と時間でなぞってタイムラインに並べる、という中身をそのまま指している。


動画から文字起こしして話者を分け（transcribe）、その結果を人が直す（edit）。
2 つで 1 つの作業なので同じパッケージに入れている。

    python -m traccia edit                       # エディタを開く
    python -m traccia transcribe 動画.mp4 --diarize --speakers 4

Mac は `Traccia.command`、Windows は `Traccia.bat` をダブルクリックしても開く。

文字起こしは torch / pyannote / faster-whisper を使うため重い。
edit のときは読み込まないよう、import はサブコマンドの中で行っている。
"""

__version__ = "1.1.0"
NAME = "Traccia"
