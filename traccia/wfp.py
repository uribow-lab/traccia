"""Filmora のプロジェクトファイル（.wfp）から字幕を取り出す。

Filmora で尺・文言・話者を直したあと、その結果を .srt に戻すためのもの。
動画を書き出したあとでも、プロジェクトファイルさえ残っていれば最終版が取れる。

    python -m traccia wfp resources/daikanyama_cafe/daikanyama_cafe.wfp
      → resources/daikanyama_cafe/export/daikanyama_cafe_wfp.srt

.wfp の中身
-----------
.wfp は ZIP。中身はだいたいこうなっている。

    ProjectFolder/project_info.json                   … timeline_mediaId でルートを指す
    ProjectFolder/Medias/<mediaId>/timeline.wesproj   … タイムライン本体（JSON）
    ProjectFolder/Medias/テロップ(名前)_1行_<時刻>/    … テロップのプリセット

timeline.wesproj の timelineInfos は「タイムラインの配列」で、入れ子シーケンスも
テロップ 1 個も、すべてこの配列に別 timelineId として並んでいる。親子は
クリップの timelineId 参照でつながる。つまり構造はこう:

    ルート(245) ─ トラック ─ クリップ(type 6/16, 入れ子シーケンス) ─→ 別タイムライン(246)
                                └ クリップ(type 7, テロップ)        ─→ 別タイムライン(248)
                                                                        └ クリップ(type 4)
                                                                           scriptBuf.Text = 本文

時刻はクリップの tlBegin / tlEnd（100ナノ秒単位）。入れ子の中の時刻は
「親クリップの tlBegin - 親クリップの inPoint」を足すとルート基準になる。

話者の見分け方
--------------
Filmora 側には話者という概念が無い。この現場では話者ごとにテロップの
プリセット（縁の色違い）を分けているので、本文の縁色をキーにして束ねる。
色が 1 種類しか無ければ、代わりにトラックで束ねる。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .editor import srt
from .editor.srt import Cue

# tlBegin / tlEnd の単位。100ナノ秒 = 1/10,000,000 秒。
TICK = 10_000_000

# clipList の type。必要なものだけ名前を付ける。
CLIP_TEXT = 4       # テロップの中身（scriptBuf に本文が入っている）
CLIP_TITLE = 7      # タイムラインに置かれたテロップ本体（中身は別タイムライン）

TRACK_AUDIO = 2     # trackType。入れ子シーケンスは映像側と音声側の両方に現れる

PROJECT_INFO = "ProjectFolder/project_info.json"
TIMELINE_NAME = "timeline.wesproj"

# 「テロップ(とーる)_1行_1772179362020」から「とーる」を取る
PRESET_NAME_RE = re.compile(r"[（(]([^）)]+)[）)]")

MAX_DEPTH = 12      # 入れ子シーケンスの掘り下げ上限（壊れたファイルで無限に潜らないよう）


class WfpError(Exception):
    pass


@dataclass
class TitleClip:
    """タイムラインに置かれたテロップ 1 個。"""
    start: float                 # ルート基準の秒
    end: float
    text: str
    color: int                   # 本文の縁色。話者の見分けに使う
    track: str                   # 例 "246.trk9"。色が使えないときのキー

    def key(self) -> tuple:
        return (round(self.start, 4), round(self.end, 4), self.text, self.color, self.track)


@dataclass
class Speaker:
    label: str
    color: int
    count: int
    first: float
    preset: str | None = None    # プリセット名から拾えた場合の名前


@dataclass
class Extraction:
    cues: list[Cue] = field(default_factory=list)
    speakers: list[Speaker] = field(default_factory=list)
    excluded: list[TitleClip] = field(default_factory=list)   # 字幕トラック外のテロップ
    warnings: list[str] = field(default_factory=list)
    project_name: str = ""
    duration: float = 0.0


# ---------------------------------------------------------------- 読み込み


def _load_timeline(zf: zipfile.ZipFile) -> tuple[dict, dict]:
    """project_info.json とルートの timeline.wesproj を読む。"""
    try:
        info = json.loads(zf.read(PROJECT_INFO))
    except KeyError as e:
        raise WfpError(f"{PROJECT_INFO} がありません。Filmora のプロジェクトではないようです") from e
    except json.JSONDecodeError as e:
        raise WfpError(f"{PROJECT_INFO} を JSON として読めません: {e}") from e

    media_id = info.get("timeline_mediaId")
    path = f"ProjectFolder/Medias/{media_id}/{TIMELINE_NAME}"
    if not media_id or path not in zf.namelist():
        # ルートの指定が壊れていても、いちばん大きい timeline.wesproj が本体のことが多い
        cands = [i for i in zf.infolist() if i.filename.endswith("/" + TIMELINE_NAME)]
        if not cands:
            raise WfpError(f"{TIMELINE_NAME} が見つかりません")
        path = max(cands, key=lambda i: i.file_size).filename

    try:
        doc = json.loads(zf.read(path))
    except json.JSONDecodeError as e:
        raise WfpError(f"{path} を JSON として読めません: {e}") from e
    return info, doc


def _preset_names(zf: zipfile.ZipFile) -> dict[int, str]:
    """テロップのプリセットから「縁色 → 名前」を作る。

    「テロップ(とーる)_1行_...」のように括弧付きの名前だけを拾う。
    同じ色に別々の名前が付いていたら、どちらとも決められないので捨てる。
    """
    found: dict[int, set[str]] = {}
    for name in zf.namelist():
        if not name.endswith("/Data/data.json"):
            continue
        folder = name.split("/")[2]
        m = PRESET_NAME_RE.search(folder.split("_")[0])
        if not m:
            continue
        try:
            data = json.loads(zf.read(name))
        except json.JSONDecodeError:
            continue
        sub = data.get("subTimeline")
        if not isinstance(sub, dict):
            continue
        for tl in sub.get("timelineInfos", []):
            for track in tl.get("trackInfos", []):
                for clip in track.get("clipList", []) or []:
                    if clip.get("type") != CLIP_TEXT:
                        continue
                    layers = _text_layers(clip)
                    if layers:
                        found.setdefault(layers[0][1], set()).add(m.group(1))
    return {color: names.pop() for color, names in found.items() if len(names) == 1}


def _text_layers(clip: dict) -> list[tuple[str, int]]:
    """テロップ本体（type 4）から (本文, 縁色) を取り出す。空の層は捨てる。"""
    raw = clip.get("scriptBuf")
    if not raw:
        return []
    try:
        script = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    text = (script.get("Text") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    data = script.get("TextData") or [{}]
    border = (data[0].get("Border") or {}).get("Color", 0)
    return [(text, int(border))]


# ---------------------------------------------------------------- 走査


class _Walker:
    def __init__(self, doc: dict):
        self.timelines = {t.get("timelineId"): t for t in doc.get("timelineInfos", [])}
        self.root_id = doc.get("currentTimelineId")
        self.titles: list[TitleClip] = []
        self.excluded: list[TitleClip] = []
        self.warnings: list[str] = []
        self._texts_cache: dict[int, list[tuple[str, int]]] = {}

    def texts_of(self, timeline_id) -> list[tuple[str, int]]:
        """そのタイムラインが直に持っているテロップ本文。入れ子の先までは見ない。"""
        if timeline_id in self._texts_cache:
            return self._texts_cache[timeline_id]
        out: list[tuple[str, int]] = []
        tl = self.timelines.get(timeline_id)
        if tl:
            for track in tl.get("trackInfos", []):
                for clip in track.get("clipList", []) or []:
                    if clip.get("type") == CLIP_TEXT:
                        out.extend(_text_layers(clip))
        self._texts_cache[timeline_id] = out
        return out

    def _title_of(self, clip: dict) -> list[tuple[str, int]]:
        """クリップがテロップなら中身を返す。違えば空。"""
        if clip.get("type") != CLIP_TITLE:
            return []
        return self.texts_of(clip.get("timelineId"))

    def _is_subtitle_track(self, clips: list[dict]) -> bool:
        """字幕トラックか。全部テロップで、全部に本文があるものだけをそう見なす。

        飾りのタイトルは映像クリップと同じトラックに置かれていることが多い。
        混ざっているトラックは字幕ではないと判断して、除外分として報告する。
        """
        return bool(clips) and all(self._title_of(c) for c in clips)

    def _speed_of(self, clip: dict) -> float | None:
        """等速なら 1.0。速度変更されていたら代表値、読めなければ None。"""
        speed = clip.get("speed")
        if not isinstance(speed, dict):
            return 1.0
        try:
            param = json.loads(speed.get("speedParam") or "{}")
        except json.JSONDecodeError:
            return None
        values = {round(float(k.get("_value", 1.0)), 6) for k in param.get("keyframeSets", [])}
        if not values:
            return 1.0
        return values.pop() if len(values) == 1 else None

    def walk(self) -> None:
        if self.root_id not in self.timelines:
            raise WfpError("ルートのタイムラインが見つかりません")
        self._walk(self.root_id, 0, 0, frozenset())

    def _walk(self, timeline_id, offset: int, depth: int, stack: frozenset) -> None:
        if timeline_id in stack or depth > MAX_DEPTH:
            return
        tl = self.timelines.get(timeline_id)
        if tl is None:
            return
        stack = stack | {timeline_id}

        for index, track in enumerate(tl.get("trackInfos", [])):
            clips = track.get("clipList") or []
            if not clips:
                continue
            # 入れ子シーケンスは映像トラックと音声トラックの両方から同じ中身を指す。
            # 音声側を辿ると全部が二重になるので、映像側だけ見る。
            if track.get("trackType") == TRACK_AUDIO:
                continue

            where = f"{timeline_id}.trk{index}"
            subtitle = self._is_subtitle_track(clips)

            for clip in clips:
                layers = self._title_of(clip)
                if layers:
                    item = TitleClip(
                        start=(offset + clip.get("tlBegin", 0)) / TICK,
                        end=(offset + clip.get("tlEnd", 0)) / TICK,
                        text="\n".join(t for t, _ in layers),
                        color=layers[0][1],
                        track=where,
                    )
                    (self.titles if subtitle else self.excluded).append(item)
                    continue

                sub = clip.get("timelineId")
                if sub is None:
                    continue
                speed = self._speed_of(clip)
                if speed is None or abs(speed - 1.0) > 1e-6:
                    self.warnings.append(
                        f"{where} の入れ子シーケンスに速度変更があります"
                        f"（{'可変' if speed is None else f'{speed}倍'}）。"
                        "その中の字幕の時刻はずれます。"
                    )
                self._walk(sub, offset + clip.get("tlBegin", 0) - clip.get("inPoint", 0),
                           depth + 1, stack)


# ---------------------------------------------------------------- 話者


def _label_stream(taken: set[str]):
    """A, B, C, ... AA, AB ... のうち、まだ使っていないものを順に返す。"""
    i = 0
    while True:
        n, name = i, ""
        while True:
            name = chr(ord("A") + n % 26) + name
            n = n // 26 - 1
            if n < 0:
                break
        i += 1
        if name not in taken:
            yield name


def _assign_speakers(titles: list[TitleClip], presets: dict[int, str],
                     override: list[str] | None) -> tuple[list[Speaker], dict, object]:
    """テロップを話者ごとに束ね、ラベルを決める。

    キーは本文の縁色。色が 1 種類しか無いのにトラックが複数あるなら、
    色では分かれていないということなのでトラックをキーにする。
    """
    colors = {t.color for t in titles}
    tracks = {t.track for t in titles}
    by_color = not (len(colors) == 1 and len(tracks) > 1)
    key_of = (lambda t: t.color) if by_color else (lambda t: t.track)

    groups: dict = {}
    for t in sorted(titles, key=lambda x: (x.start, x.end)):
        groups.setdefault(key_of(t), []).append(t)

    # 最初に出てきた順に並べる
    order = sorted(groups, key=lambda k: groups[k][0].start)

    names = list(override or [])
    if names and len(names) != len(order):
        raise WfpError(
            f"--speakers に {len(names)} 人ぶん指定されましたが、"
            f"このプロジェクトの話者は {len(order)} 人です"
        )

    taken = set(names)
    labels = _label_stream(taken)
    speakers: list[Speaker] = []
    for i, key in enumerate(order):
        group = groups[key]
        preset = presets.get(group[0].color)
        if names:
            label = names[i]
        elif preset and preset not in taken:
            label = preset
            taken.add(label)
        else:
            label = next(labels)
        speakers.append(Speaker(label=label, color=group[0].color,
                                count=len(group), first=group[0].start, preset=preset))

    return speakers, {key: sp.label for key, sp in zip(order, speakers)}, key_of


# ---------------------------------------------------------------- 入口


def extract(path: Path, speaker_names: list[str] | None = None) -> Extraction:
    """.wfp から字幕を取り出す。"""
    if not path.is_file():
        raise WfpError(f"ファイルがありません: {path}")
    if not zipfile.is_zipfile(path):
        raise WfpError(f"{path.name} は ZIP ではありません。.wfp ではないようです")

    with zipfile.ZipFile(path) as zf:
        info, doc = _load_timeline(zf)
        presets = _preset_names(zf)

    walker = _Walker(doc)
    walker.walk()

    # 同じテロップが二重に拾われていないか最後に確かめる（保険）
    seen: set[tuple] = set()
    titles: list[TitleClip] = []
    for t in walker.titles:
        if t.key() in seen:
            continue
        seen.add(t.key())
        titles.append(t)

    result = Extraction(
        excluded=walker.excluded,
        warnings=list(walker.warnings),
        project_name=str(info.get("project_file_name") or path.stem),
        duration=float(info.get("project_timeline_duration") or 0) / TICK,
    )
    if not titles:
        result.warnings.append("字幕トラックが見つかりませんでした")
        return result

    speakers, label_of, key_of = _assign_speakers(titles, presets, speaker_names)
    result.speakers = speakers

    cues = [
        Cue(start=t.start, end=t.end, speaker=label_of[key_of(t)], text=t.text)
        for t in titles
    ]
    cues.sort(key=lambda c: (c.start, c.end))
    for i, c in enumerate(cues, start=1):
        c.id = i
    result.cues = cues

    # 同じ話者の字幕が重なっていたら、拾い方を間違えている可能性がある
    for sp in speakers:
        mine = [c for c in cues if c.speaker == sp.label]
        overlaps = sum(1 for a, b in zip(mine, mine[1:]) if b.start < a.end - 1e-6)
        if overlaps:
            result.warnings.append(f"話者{sp.label}: 重なっている字幕が {overlaps} 件あります")

    return result


def default_output(wfp: Path) -> Path:
    """resources/x/x.wfp → resources/x/export/x_wfp.srt"""
    return wfp.parent / "export" / f"{wfp.stem}_wfp.srt"


def write(result: Extraction, out: Path, per_speaker: bool = False) -> list[Path]:
    """本体（話者プレフィクス付き）を書く。per_speaker なら話者別も並べて出す。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(srt.serialize(result.cues, with_speaker_prefix=True), encoding="utf-8")
    written = [out]

    if per_speaker:
        for sp in result.speakers:
            group = [c for c in result.cues if c.speaker == sp.label and c.text.strip()]
            if not group:
                continue
            # 動画の先頭に合わせるため、最初の発話までを本文なしのブロックで埋める。
            # 話者別 .srt の既存の形式（project.export / transcribe.py）に合わせる。
            first = min(c.start for c in group)
            if first > 0.001:
                group = [Cue(start=0.0, end=first, speaker=sp.label, text="")] + group
            target = out.with_name(f"{out.stem}.話者{sp.label}.srt")
            target.write_text(srt.serialize(group), encoding="utf-8")
            written.append(target)

    return written


def _pad(text: str, width: int) -> str:
    """全角を 2 文字ぶんとして桁を合わせる。話者名に日本語が入るため。"""
    used = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(1, width - used)


def _report(result: Extraction, written: list[Path]) -> None:
    print(f"プロジェクト: {result.project_name}")
    print(f"尺: {srt.format_timestamp(result.duration)}  字幕: {len(result.cues)} 件")
    print()
    print(f"  {_pad('話者', 12)}件数  最初の発話    縁色      プリセット名")
    for sp in result.speakers:
        print(f"  {_pad('話者' + sp.label, 12)}{sp.count:>4}  "
              f"{srt.format_timestamp(sp.first)}  #{sp.color:06X}  {sp.preset or '-'}")
    print()

    if result.excluded:
        print(f"字幕トラック外のテロップを {len(result.excluded)} 件除きました"
              "（飾りのタイトルとみなしたもの）:")
        for t in result.excluded[:10]:
            head = t.text.replace("\n", " ")[:40]
            print(f"  {srt.format_timestamp(t.start)}  {t.track}  {head}")
        if len(result.excluded) > 10:
            print(f"  ... 他 {len(result.excluded) - 10} 件")
        print("  必要なら --include-all で一緒に書き出せます。")
        print()

    for w in result.warnings:
        print(f"注意: {w}", file=sys.stderr)

    for p in written:
        print(f"書き出し: {p}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m traccia wfp",
        description="Filmora のプロジェクト（.wfp）から最終版の字幕を .srt に取り出す",
    )
    ap.add_argument("wfp", type=Path, help="Filmora のプロジェクトファイル（.wfp）")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="出力先（既定: <.wfp と同じ場所>/export/<名前>_wfp.srt）")
    ap.add_argument("--per-speaker", action="store_true",
                    help="話者別の .srt も一緒に書き出す")
    ap.add_argument("--speakers", default=None,
                    help="話者名をカンマ区切りで指定する（最初に出てきた順）。"
                         "例: --speakers とーる,タケ,でこ,カメラマン")
    ap.add_argument("--include-all", action="store_true",
                    help="字幕トラック外のテロップ（飾りのタイトルなど）も含める")
    ap.add_argument("--dry-run", action="store_true", help="内容だけ表示して書き出さない")
    args = ap.parse_args(argv)

    names = [n.strip() for n in args.speakers.split(",")] if args.speakers else None
    if names and not all(names):
        print("--speakers に空の名前があります", file=sys.stderr)
        return 2

    try:
        result = extract(args.wfp.expanduser(), speaker_names=names)
    except WfpError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    if args.include_all and result.excluded:
        # 話者は分からないので不明扱いで足す
        for t in result.excluded:
            result.cues.append(Cue(start=t.start, end=t.end, speaker=srt.UNKNOWN, text=t.text))
        result.cues.sort(key=lambda c: (c.start, c.end))
        for i, c in enumerate(result.cues, start=1):
            c.id = i
        result.excluded = []

    if not result.cues:
        for w in result.warnings:
            print(f"注意: {w}", file=sys.stderr)
        print("字幕が 1 件も取れませんでした", file=sys.stderr)
        return 1

    out = (args.output or default_output(args.wfp)).expanduser()
    written = [] if args.dry_run else write(result, out, per_speaker=args.per_speaker)
    _report(result, written)
    if args.dry_run:
        print(f"（--dry-run のため書き出していません。出力先は {out}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
