"""波形トラックのエディタ側の入口。

中身は traccia/peaks.py。文字起こし側（transcribe.py）と同じコードを使う。
transcribe.py は話者分離のワーカーとして単体スクリプトのまま起動されるため、
共有するモジュールはパッケージ相対 import を持てない。それが peaks.py を
1 階層上に置いている理由。

ピークファイルは <セット>/<名前>.peaks.json。形式は peaks.py の docstring 参照。

本筋は文字起こし時に一緒に作ること（音声のデコードが済んでいるのでほぼタダ）。
ここの extract() は、すでに文字起こし済みの素材にあとから足すためのもので、
動画を読み直すぶん時間がかかる。
"""

from __future__ import annotations

from pathlib import Path

from .. import peaks as _peaks
from ..peaks import DEFAULT_RATE, PEAKS_VERSION, NotExtracted, PeaksError  # noqa: F401


class ExtractionUnavailable(Exception):
    """抽出に必要なものが揃っていない。"""


def load(peaks_file: Path) -> dict:
    return _peaks.load(peaks_file)


def save(peaks_file: Path, data: dict) -> None:
    _peaks.save(peaks_file, data)


def extract(video: Path, peaks_file: Path, rate: int = DEFAULT_RATE) -> dict:
    """動画から音声を読んでピーク列を作り、peaks_file に保存する。"""
    try:
        data = _peaks.extract(video, rate=rate)
    except PeaksError as e:
        raise ExtractionUnavailable(str(e)) from e
    _peaks.save(peaks_file, data)
    return data
