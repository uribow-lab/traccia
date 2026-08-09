"""SRT の読み書き。

transcribe.py が出力する .srt は本文の先頭に「話者A: 」のような
話者プレフィクスが付く。ここではそれを本文から切り離して扱う。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# 「話者A: 」「話者不明：」「話者とーる: 」など。全角コロンと空白揺れを許容する。
# 話者名は A/B/C とは限らない（設定で改名できるし、.wfp から取ると日本語になる）ので
# コロンまでを名前として受ける。書き出し側と同じ形を読めるようにしておく。
SPEAKER_RE = re.compile(r"^\s*話者\s*([^\s:：]{1,24})\s*[:：]\s*")

TIME_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)

UNKNOWN = "不明"

# 本文の中の空行。SRT は空行をブロックの区切りに使うので、本文に空行があると
# そこで切れてしまい、後ろが読めなくなる（malformed として捨てられる）。
# 改行そのものは 2 行字幕として正しいので、空行だけを畳む。
BLANK_LINES_RE = re.compile(r"\n\s*\n+")


def normalize_text(text: str) -> str:
    """本文を SRT に載せられる形にそろえる。

    ・改行コードを \n にそろえる（Windows で編集したものが混ざるため）
    ・空行を 1 つの改行に畳む（これをしないと書き出し後に本文が失われる）
    ・前後の空白と改行を落とす
    """
    if not text:
        return ""
    t = str(text).replace("\r\n", "\n").replace("\r", "\n")
    t = BLANK_LINES_RE.sub("\n", t)
    # 各行の行末の空白は見えないまま残るので落とす
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    return t.strip()


@dataclass
class Cue:
    start: float
    end: float
    speaker: str = UNKNOWN
    text: str = ""
    id: int = 0
    # 編集する人が自分で意味を決めて使う目印。「あとで見直す」「ここまで済み」など。
    # 編集の途中経過なので .srt には書き出さない（置く場所も無い）。
    mark: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "speaker": self.speaker,
            "text": self.text,
        }
        # 付いているときだけ書く。既存の edit.json と互換が保て、
        # 印を使わない人のファイルも太らない
        if self.mark:
            d["mark"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Cue":
        return cls(
            start=float(d["start"]),
            end=float(d["end"]),
            speaker=d.get("speaker") or UNKNOWN,
            # 画面や文字起こしから来た本文はここで整える。
            # 空行のまま保存すると、書き出したあと読み直せなくなる
            text=normalize_text(d.get("text") or ""),
            id=int(d.get("id") or 0),
            mark=bool(d.get("mark")),
        )


@dataclass
class ParseReport:
    """読み込み時に落としたもの・直したものの記録。UI に出して黙って捨てない。"""

    total_blocks: int = 0
    empty_dropped: int = 0
    malformed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_blocks": self.total_blocks,
            "empty_dropped": self.empty_dropped,
            "malformed": self.malformed[:20],
        }


def parse_timestamp(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def format_timestamp(t: float) -> str:
    if t < 0:
        t = 0.0
    total_ms = int(round(t * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def split_speaker(text: str) -> tuple[str | None, str]:
    """本文から話者プレフィクスを剥がす。無ければ (None, text)。"""
    m = SPEAKER_RE.match(text)
    if not m:
        return None, text.strip()
    return m.group(1), text[m.end():].strip()


def parse(content: str, default_speaker: str | None = None) -> tuple[list[Cue], ParseReport]:
    """SRT 文字列を Cue のリストにする。

    本文が空のブロック（抽出器が隙間埋めに入れる巨大な空 cue）は落とす。
    落とした件数は ParseReport に残す。
    """
    report = ParseReport()
    cues: list[Cue] = []

    content = content.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    for raw in re.split(r"\n{2,}", content):
        block = raw.strip("\n")
        if not block.strip():
            continue
        report.total_blocks += 1

        lines = block.split("\n")
        # 先頭が通し番号ならスキップ
        idx = 0
        if lines and lines[0].strip().isdigit():
            idx = 1
        if idx >= len(lines):
            report.malformed.append(block[:60])
            continue

        m = TIME_RE.search(lines[idx])
        if not m:
            report.malformed.append(block[:60])
            continue

        start = parse_timestamp(*m.groups()[0:4])
        end = parse_timestamp(*m.groups()[4:8])
        body = "\n".join(lines[idx + 1:]).strip()

        if not body:
            report.empty_dropped += 1
            continue

        spk, text = split_speaker(body)
        cues.append(Cue(start=start, end=end, speaker=spk or default_speaker or UNKNOWN, text=text))

    cues.sort(key=lambda c: (c.start, c.end))
    return cues, report


def serialize(cues: Iterable[Cue], with_speaker_prefix: bool = False) -> str:
    """SRT 文字列にする。通し番号は 1 から振り直す。"""
    out: list[str] = []
    for i, c in enumerate(sorted(cues, key=lambda x: (x.start, x.end)), start=1):
        # 書き出す直前にもう一度そろえる。ここを通らない経路で
        # 空行が入っていても、ファイルは壊さない
        body = normalize_text(c.text)
        if with_speaker_prefix:
            # 2 行字幕でも、話者は 1 行目にだけ付ける
            body = f"話者{c.speaker}: {body}"
        out.append(
            f"{i}\n{format_timestamp(c.start)} --> {format_timestamp(c.end)}\n{body}\n"
        )
    return "\n".join(out)
