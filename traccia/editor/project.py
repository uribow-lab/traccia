"""セットフォルダ（動画 + .srt 群）の探索・読み込み・保存・書き出し。

原本の .srt には一切書き戻さない。
  編集内容 : <set>/<stem>.edit.json
  書き出し : <set>/export/<stem>.話者X.srt
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import srt
from .srt import UNKNOWN, Cue

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v", ".webm")
PROJECT_VERSION = 1

# edit.json がどこから来たか。フィードバック（TRAC-23/24）で
# 「機械が出したもの」と「人が直したもの」を突き合わせるために要る。
ORIGIN_TRANSCRIBE = "transcribe"   # 文字起こしの生出力
ORIGIN_MANUAL = "manual"           # 人が直し切った確定版
ORIGIN_WFP = "wfp"                 # Filmora から取り戻した最終版

# 残す版と、その説明。画面にもこの順で出す。
GENERATIONS = [
    {"kind": "auto",   "origin": ORIGIN_TRANSCRIBE, "label": "文字起こしの生出力"},
    {"kind": "manual", "origin": ORIGIN_MANUAL,     "label": "手作業の確定版"},
    {"kind": "wfp",    "origin": ORIGIN_WFP,        "label": "wfp の最終版"},
]

# 「princi.話者A.srt」の 話者A 部分。改名後の日本語名（princi.話者たけ.srt）も拾う
PER_SPEAKER_RE = re.compile(r"^(?P<stem>.+)\.話者(?P<spk>[^.]+)$")

DEFAULT_FPS = 24000 / 1001  # 23.976


class ProjectError(Exception):
    pass


@dataclass
class MediaInfo:
    duration: float = 0.0
    fps: float = DEFAULT_FPS
    width: int = 0
    height: int = 0
    vcodec: str = ""
    acodec: str = ""
    probed: bool = False


def probe(video: Path) -> MediaInfo:
    """PyAV でメタデータを読む。PyAV が無ければ既定値で続行する。"""
    info = MediaInfo()
    try:
        import av  # type: ignore
    except Exception:
        return info
    try:
        with av.open(str(video)) as c:
            if c.duration:
                info.duration = c.duration / 1_000_000
            for s in c.streams:
                if s.type == "video":
                    cc = s.codec_context
                    info.width, info.height = cc.width, cc.height
                    info.vcodec = cc.name
                    if s.average_rate:
                        info.fps = float(s.average_rate)
                elif s.type == "audio":
                    info.acodec = s.codec_context.name
        info.probed = True
    except Exception:
        pass
    return info


@dataclass
class SetPaths:
    name: str
    root: Path
    video: Path
    stem: str
    main_srt: Path | None
    speaker_srts: dict[str, Path]

    @property
    def project_file(self) -> Path:
        return self.root / f"{self.stem}.edit.json"

    def generation_file(self, kind: str) -> Path:
        """性質のちがう版の置き場。edit.json は現役の編集データのまま。

            auto   … 文字起こしの生出力。流した瞬間の姿
            manual … 人が直し切った確定版
            wfp    … Filmora のプロジェクトから取り戻した最終版
        """
        return self.root / f"{self.stem}.{kind}.edit.json"

    @property
    def auto_file(self) -> Path:
        return self.generation_file("auto")

    @property
    def manual_file(self) -> Path:
        return self.generation_file("manual")

    @property
    def wfp_file(self) -> Path:
        return self.generation_file("wfp")

    @property
    def settings_file(self) -> Path:
        """話者の名前と色。セットごとの設定。"""
        return self.root / f"{self.stem}.settings.json"

    @property
    def export_dir(self) -> Path:
        return self.root / "export"

    @property
    def backup_dir(self) -> Path:
        """世代バックアップの置き場。セットフォルダ直下が散らからないよう分ける。"""
        return self.root / "backup"

    @property
    def waveform_file(self) -> Path:
        return self.root / f"{self.stem}.peaks.json"

    @property
    def worklog_file(self) -> Path:
        """このセットの編集にかけた時間。見積もりの材料。"""
        return self.root / f"{self.stem}.worklog.json"


def discover_set(folder: Path) -> SetPaths | None:
    """フォルダ 1 つを 1 セットとして解釈する。動画が無ければ None。"""
    if not folder.is_dir():
        return None

    videos = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")
    )
    if not videos:
        return None
    video = videos[0]
    stem = video.stem

    main_srt: Path | None = None
    speaker_srts: dict[str, Path] = {}
    for p in sorted(folder.glob("*.srt")):
        if p.name.startswith("."):
            continue
        m = PER_SPEAKER_RE.match(p.stem)
        if m:
            speaker_srts[m.group("spk")] = p
        elif p.stem == stem:
            main_srt = p
    # 動画名と揃っていなくても、話者別でない .srt が 1 本だけならそれを本体とみなす
    if main_srt is None:
        plain = [p for p in sorted(folder.glob("*.srt"))
                 if not PER_SPEAKER_RE.match(p.stem) and not p.name.startswith(".")]
        if len(plain) == 1:
            main_srt = plain[0]

    return SetPaths(
        name=folder.name, root=folder, video=video, stem=stem,
        main_srt=main_srt, speaker_srts=speaker_srts,
    )


def list_sets(resources: Path) -> list[SetPaths]:
    if not resources.is_dir():
        return []
    out = []
    for child in sorted(resources.iterdir()):
        if child.name.startswith("."):
            continue
        s = discover_set(child)
        if s:
            out.append(s)
    return out


BACKUP_KEEP = 5
LEAD_EPS = 0.001       # これ未満の頭出しは埋めない（実質 0 秒開始）


def _rotate_backups(path: Path, backup_dir: Path, keep: int = BACKUP_KEEP) -> None:
    """上書きする前に、いまのファイルを世代バックアップへ退避する。

        <セット>/<name>.json          → <セット>/backup/<name>.json.1
        <セット>/backup/<name>.json.1 → <セット>/backup/<name>.json.2
        （以下ずらす。keep 世代でいちばん古いを捨てる）

    事故で消したり、意図しない上書きをしたときに 1 つ前へ戻せるようにする。
    セットフォルダが散らからないよう backup/ にまとめる。
    バックアップ自体は .gitignore で除外している。
    """
    if not path.exists():
        return
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        gen = lambda i: backup_dir / f"{path.name}.{i}"   # noqa: E731

        oldest = gen(keep)
        if oldest.exists():
            oldest.unlink()
        for i in range(keep - 1, 0, -1):
            src = gen(i)
            if src.exists():
                src.replace(gen(i + 1))
        path.replace(gen(1))
    except OSError:
        # バックアップに失敗しても保存そのものは続ける
        pass


def _renumber(cues: list[Cue]) -> int:
    cues.sort(key=lambda c: (c.start, c.end))
    for i, c in enumerate(cues, start=1):
        c.id = i
    return len(cues) + 1


def load_from_srt(paths: SetPaths) -> tuple[list[Cue], dict]:
    """原本の .srt から Cue を作る。

    本体 .srt があればそれを使う（話者プレフィクス付き）。
    無ければ話者別 .srt をマージする。
    どちらも無いときは空で返す。動画だけ置いて、エディタから文字起こしを
    始める使い方があるため、ここでは失敗にしない。
    """
    reports: dict = {}
    cues: list[Cue] = []

    if paths.main_srt and paths.main_srt.exists():
        text = paths.main_srt.read_text(encoding="utf-8", errors="replace")
        cues, rep = srt.parse(text)
        reports[paths.main_srt.name] = rep.to_dict()
    elif paths.speaker_srts:
        for spk, p in sorted(paths.speaker_srts.items()):
            text = p.read_text(encoding="utf-8", errors="replace")
            part, rep = srt.parse(text, default_speaker=spk)
            reports[p.name] = rep.to_dict()
            cues.extend(part)

    _renumber(cues)
    return cues, reports


def speakers_of(cues: list[Cue], paths: SetPaths | None = None) -> list[str]:
    """レーンに出す話者の並び。A,B,C,D… を先に、不明は末尾。"""
    found = {c.speaker for c in cues}
    if paths:
        found |= set(paths.speaker_srts.keys())
    named = sorted(s for s in found if s != UNKNOWN)
    return named + ([UNKNOWN] if UNKNOWN in found or not named else [])


# 話者に自動で割り当てる色。設定ファイルが無いときの既定。
DEFAULT_PALETTE = [
    "#E4685D", "#5B6EE1", "#E0A93F", "#A97BD4",
    "#4FB477", "#D96BA8", "#5FB3C9", "#B79A5E",
]
UNKNOWN_COLOR = "#6E8189"
SETTINGS_VERSION = 1


def default_color(name: str, index: int) -> str:
    if name == UNKNOWN:
        return UNKNOWN_COLOR
    return DEFAULT_PALETTE[index % len(DEFAULT_PALETTE)]


def load_settings_doc(paths: SetPaths) -> dict:
    """settings.json をそのまま読む。壊れていても落とさず空扱いにする。"""
    if not paths.settings_file.exists():
        return {}
    try:
        data = json.loads(paths.settings_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def clean_terms(raw) -> list[str]:
    """用語リストの正規化。改行区切りでも配列でも受ける。

    順序は書いた順のまま保つ。重複だけ落とす。
    """
    if isinstance(raw, str):
        raw = re.split(r"[\n,、]+", raw)
    out, seen = [], set()
    for w in raw or []:
        w = str(w).strip()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def load_vocab(paths: SetPaths) -> dict:
    """このセットの固有名詞と場面説明。文字起こしに渡すためのもの。"""
    data = load_settings_doc(paths)
    return {
        "terms": clean_terms(data.get("terms")),
        "note": str(data.get("note") or "").strip(),
    }


def load_settings(paths: SetPaths) -> list[dict]:
    """[{"name": "A", "color": "#E4685D"}, ...] を返す。無ければ空リスト。"""
    data = load_settings_doc(paths)
    if not data:
        return []
    out = []
    for s in data.get("speakers", []):
        name = str(s.get("name") or "").strip()
        if not name:
            continue
        color = str(s.get("color") or "").strip()
        out.append({"name": name, "color": color or default_color(name, len(out))})
    return out


def save_settings(paths: SetPaths, payload: dict) -> dict:
    # 既存の内容を土台にする。payload に無い項目を書き落とさないため。
    # 話者だけ送ってくる古い呼び出しで用語が消える、という事故を防ぐ。
    prev = load_settings_doc(paths)

    speakers = []
    seen: set[str] = set()
    for s in payload.get("speakers", []):
        name = str(s.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        color = str(s.get("color") or "").strip() or default_color(name, len(speakers))
        speakers.append({"name": name, "color": color})

    doc = {
        "version": SETTINGS_VERSION,
        "name": paths.name,
        "stem": paths.stem,
        "savedAt": time.time(),
        "speakers": speakers if speakers else prev.get("speakers", []),
        # 文字起こしに渡す手がかり。キーが来ていなければ前の値を残す
        "terms": clean_terms(payload["terms"]) if "terms" in payload
                 else clean_terms(prev.get("terms")),
        "note": (str(payload.get("note") or "").strip() if "note" in payload
                 else str(prev.get("note") or "").strip()),
    }
    _rotate_backups(paths.settings_file, paths.backup_dir)
    tmp = paths.settings_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(paths.settings_file)
    return {"ok": True, "path": str(paths.settings_file),
            "speakers": doc["speakers"], "terms": doc["terms"], "note": doc["note"]}


def resolve_speakers(cues: list[Cue], paths: SetPaths) -> tuple[list[str], dict[str, str]]:
    """レーンに出す話者の並びと色。

    設定ファイルがあればその並び・色が正。話者名を変えたあとに、元の
    話者別 .srt のファイル名から古い名前が復活しないよう、設定がある場合は
    ファイル名を手がかりにしない。
    """
    settings = load_settings(paths)
    if settings:
        order = [s["name"] for s in settings]
        colors = {s["name"]: s["color"] for s in settings}
        for name in sorted({c.speaker for c in cues}):
            if name not in colors:
                order.append(name)
                colors[name] = default_color(name, len(colors))
    else:
        # 初回は話者別 .srt のファイル名も手がかりにして、空のレーンも用意する
        order = speakers_of(cues, paths)
        colors = {n: default_color(n, i) for i, n in enumerate(order)}

    if UNKNOWN in order:
        order = [n for n in order if n != UNKNOWN] + [UNKNOWN]
    return order, colors


def load(paths: SetPaths) -> dict:
    """編集中プロジェクトがあればそれを、無ければ原本 .srt を読む。"""
    reports: dict = {}
    source = "srt"

    if paths.project_file.exists():
        try:
            data = json.loads(paths.project_file.read_text(encoding="utf-8"))
            cues = [Cue.from_dict(d) for d in data.get("cues", [])]
            source = "project"
        except Exception as e:
            raise ProjectError(f"{paths.project_file.name} の読み込みに失敗: {e}") from e
    else:
        cues, reports = load_from_srt(paths)
        # 動画だけのセット。文字起こしを流せば埋まる
        if not cues and not paths.main_srt and not paths.speaker_srts:
            source = "empty"

    info = probe(paths.video)
    duration = info.duration or (max((c.end for c in cues), default=0.0) + 5.0)
    speakers, colors = resolve_speakers(cues, paths)

    return {
        "version": PROJECT_VERSION,
        "name": paths.name,
        "stem": paths.stem,
        "source": source,
        "video": paths.video.name,
        "videoUrl": f"/media/{paths.name}",
        "duration": round(duration, 3),
        "fps": round(info.fps, 6),
        "width": info.width,
        "height": info.height,
        "vcodec": info.vcodec,
        "acodec": info.acodec,
        "probed": info.probed,
        "speakers": speakers,
        "speakerColors": colors,
        "hasSettings": paths.settings_file.exists(),
        **load_vocab(paths),
        "cues": [c.to_dict() for c in cues],
        "reports": reports,
        "hasWaveform": paths.waveform_file.exists(),
        "savedAt": (
            paths.project_file.stat().st_mtime if paths.project_file.exists() else None
        ),
    }


def read_origin(path: Path) -> dict:
    """その版がどこから来たかを読む。

    origin を持たない古いファイルは **手作業の確定版として扱う**。判断が付かない
    ものを機械の出力とみなすと、次の取り込みで手作業が退避されずに消える。
    安全side に倒す。
    """
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        "origin": str(d.get("origin") or ORIGIN_MANUAL),
        "originAt": d.get("originAt"),
        "savedAt": d.get("savedAt"),
        "count": len(d.get("cues") or []),
        "hasOrigin": bool(d.get("origin")),
    }


def save(paths: SetPaths, payload: dict) -> dict:
    """編集内容を <stem>.edit.json に書く。原本には触らない。"""
    cues = [Cue.from_dict(d) for d in payload.get("cues", [])]
    _renumber(cues)

    # origin は書き換えの指示が来たときだけ変える。人が本文を直しただけで
    # 「機械の出力」から「手作業版」に変わってしまうと、どちらを退避すべきかが
    # 分からなくなる。版の切り替えは import_segments と mark_manual だけが行う。
    prev = read_origin(paths.project_file)
    origin = str(payload.get("origin") or prev.get("origin") or ORIGIN_MANUAL)
    origin_at = payload.get("originAt") or prev.get("originAt") or time.time()

    doc = {
        "version": PROJECT_VERSION,
        "name": paths.name,
        "stem": paths.stem,
        "video": paths.video.name,
        "savedAt": time.time(),
        "origin": origin,
        "originAt": origin_at,
        "speakers": payload.get("speakers") or speakers_of(cues, paths),
        "cues": [c.to_dict() for c in cues],
    }
    _rotate_backups(paths.project_file, paths.backup_dir)
    tmp = paths.project_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(paths.project_file)
    return {"ok": True, "path": str(paths.project_file), "count": len(cues)}


def snapshot(paths: SetPaths, kind: str) -> dict:
    """いまの edit.json を、指定の版として写す。

    上書きの前に backup/ へ回すので、写し先に何かあっても失われない。
    """
    src = paths.project_file
    if not src.exists():
        return {"ok": False, "reason": "編集データがありません"}
    dst = paths.generation_file(kind)
    _rotate_backups(dst, paths.backup_dir)
    try:
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "path": str(dst), **read_origin(dst)}


def ensure_manual_snapshot(paths: SetPaths) -> dict:
    """手作業の確定版を、条件を満たすときだけ 1 回だけ写す。

    wfp を取り込むと edit.json は丸ごと差し替わる。そのとき人が直し切った版が
    backup/ の 5 世代に流れて消えてしまうので、その前にここで退避する。

    写すのは次の両方を満たすときだけ:
      ・manual.edit.json がまだ無い
      ・いまの edit.json の origin が wfp でない

    2 回目以降の wfp 取り込みでは、いまの edit.json は 1 回目の wfp 抽出結果なので
    写さない。これで「手作業版 1 つ + wfp 版 1 つ」が常に並ぶ。
    """
    if paths.manual_file.exists():
        return {"ok": False, "reason": "すでに手作業の確定版があります",
                "skipped": True}
    cur = read_origin(paths.project_file)
    if cur.get("origin") == ORIGIN_WFP:
        return {"ok": False, "reason": "いまの編集データは wfp から取り込んだものです",
                "skipped": True}
    return snapshot(paths, "manual")


def mark_manual(paths: SetPaths) -> dict:
    """いまの状態を手作業の確定版として置き直す。

    やり直しのための出口。人が押したときだけ動く。
    """
    doc = json.loads(paths.project_file.read_text(encoding="utf-8"))
    doc["origin"] = ORIGIN_MANUAL
    doc["originAt"] = time.time()
    _rotate_backups(paths.project_file, paths.backup_dir)
    tmp = paths.project_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(paths.project_file)
    _rotate_backups(paths.manual_file, paths.backup_dir)
    paths.manual_file.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    return {"ok": True, "count": len(doc.get("cues") or [])}


def generations(paths: SetPaths) -> dict:
    """いまの版と、残っている版の一覧。画面に出す。"""
    cur = read_origin(paths.project_file)
    out = []
    for g in GENERATIONS:
        f = paths.generation_file(g["kind"])
        info = read_origin(f) if f.exists() else {}
        out.append({**g, "exists": f.exists(), "file": f.name, **info})
    return {"current": cur, "generations": out}


def import_segments(paths: SetPaths, segments: list[dict],
                    origin: str = ORIGIN_TRANSCRIBE,
                    keep_as: str | None = "auto") -> dict:
    """文字起こしの結果で edit.json を丸ごと差し替える。

    元の .srt には触らない。いまの edit.json は _rotate_backups で
    backup/ に退避されるので、流し直して気に入らなければ戻せる。
    ⌘Z では戻らない（ブラウザ側の履歴ではなくファイルの入れ替えなので）。

    keep_as を渡すと、書いた直後の姿をその版としても残す。文字起こしの生出力は
    ここでしか手に入らない（4 秒ごとの自動保存が始まると 20 秒ほどで上書きされ、
    backup/ の 5 世代からも押し出される）。
    """
    cues: list[Cue] = []
    for s in segments:
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        cues.append(Cue(
            start=max(0.0, float(s.get("start") or 0.0)),
            end=float(s.get("end") or 0.0),
            speaker=str(s.get("speaker") or UNKNOWN).strip() or UNKNOWN,
            text=text,
            # 目印は落とさない。合成（traccia/merge.py）が「機械では決めきれ
            # なかった」箇所に付けてくるので、そのまま行頭に出して絞り込める
            # ようにする。付いていなければ従来どおり何も書かれない。
            mark=bool(s.get("mark")),
        ))
    _renumber(cues)

    order = speakers_of(cues)
    res = save(paths, {"speakers": order,
                       "cues": [c.to_dict() for c in cues],
                       "origin": origin, "originAt": time.time()})
    if keep_as:
        snapshot(paths, keep_as)
    return {**res, "speakers": order, "origin": origin}


def export(paths: SetPaths, payload: dict) -> dict:
    """話者ごとの .srt を export/ に書き出す。原本は残す。"""
    cues = [Cue.from_dict(d) for d in payload.get("cues", [])]
    _renumber(cues)

    out_dir = paths.export_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[dict] = []

    by_speaker: dict[str, list[Cue]] = {}
    for c in cues:
        if not c.text.strip():
            continue
        by_speaker.setdefault(c.speaker, []).append(c)

    order, _ = resolve_speakers(cues, paths)
    skipped: list[str] = []
    for spk in order:
        group = by_speaker.get(spk, [])
        if not group:
            skipped.append(spk)      # 字幕が 1 件も無い話者は空ファイルを作らない
            continue

        # 動画の先頭に合わせるため、最初の発話までを本文なしのブロックで埋める。
        # これが無いと、読み込む側が 1 本目の字幕の開始を 0 秒と解釈して全体がずれる。
        # transcribe.py の話者別出力も同じ形式。
        out_cues = list(group)
        first = min(c.start for c in out_cues)
        lead = first > LEAD_EPS
        if lead:
            out_cues.insert(0, Cue(start=0.0, end=first, speaker=spk, text=""))

        target = out_dir / f"{paths.stem}.話者{spk}.srt"
        target.write_text(srt.serialize(out_cues), encoding="utf-8")
        written.append({
            "speaker": spk, "path": str(target),
            "count": len(group), "lead": round(first, 3) if lead else 0,
        })

    # 全体版（話者プレフィクス付き）も一緒に出しておくと差分確認が楽
    whole = out_dir / f"{paths.stem}.srt"
    whole.write_text(srt.serialize(cues, with_speaker_prefix=True), encoding="utf-8")
    written.append({"speaker": "（全体）", "path": str(whole), "count": len(cues)})

    return {"ok": True, "dir": str(out_dir), "files": written, "skipped": skipped}
