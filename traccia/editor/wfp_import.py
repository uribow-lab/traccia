"""Filmora のプロジェクト（.wfp）を、エディタから取り込む。

抽出そのものは traccia/wfp.py が持っている。ここはその手前と後ろ——
どの .wfp を使うか、話者をどう対応付けるか、取り込む前に何を見せるか——を扱う。

黙って差し替えない
------------------
取り込むと edit.json は丸ごと差し替わる。人が何時間もかけて直した結果が
入っていることがあるので、押した瞬間に消えるような作りにはしない。
まず中身を見せて、それを見たうえでもう一度押したときだけ差し替える。

手作業版は取り込みの直前に退避する（project.ensure_manual_snapshot）。
条件を満たすときだけ 1 回だけ写るので、2 回目以降の取り込みでも潰れない。

話者の対応付け
--------------
Filmora 側に話者という概念は無く、本文の縁色で束ねている（wfp.py の docstring）。
こちらの settings.json は話者ごとの色を #RRGGBB で持っているので、**色が一致すれば
その名前を当てられる**。実際 yellow_company では縁色と話者色が一致していた。
一致しないものは件数の多い順に仮当てして、画面で直せるようにする。
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import wfp
from . import project
from .project import SetPaths

# 件数がこの割合を下回ったら「減りすぎ」として警告する。
# 字幕トラックの判定を外していると、ごっそり落ちることがある。
SHRINK_RATIO = 0.5


class ImportError_(Exception):
    """呼び出し側に見せるエラー。文面はそのまま画面に出る。"""


def find_wfps(paths: SetPaths) -> list[dict]:
    """セットフォルダの .wfp を、更新日時の新しい順に。"""
    out = []
    for p in sorted(paths.root.glob("*.wfp")):
        if p.name.startswith("."):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "size": st.st_size, "modified": st.st_mtime})
    out.sort(key=lambda d: -d["modified"])
    return out


def _resolve(paths: SetPaths, name: str) -> Path:
    """セットフォルダの中の .wfp だけを受け付ける。"""
    target = (paths.root / name).resolve()
    if target.parent != paths.root.resolve() or target.suffix.lower() != ".wfp":
        raise ImportError_(f"このセットの .wfp ではありません: {name}")
    if not target.is_file():
        raise ImportError_(f"ファイルがありません: {name}")
    return target


def _count_clips(path: Path) -> dict[int, int]:
    """.wfp の中のクリップを種類ごとに数える。0 件だった理由を出すため。

        1 … 映像   2 … 音声   7 … テロップ

    抽出そのものは wfp.py の仕事なので、ここでは数えるだけ。
    """
    import json
    import zipfile
    from collections import Counter
    types: Counter = Counter()
    try:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if not n.endswith(wfp.TIMELINE_NAME):
                    continue
                doc = json.loads(z.read(n))
                stack = [doc]
                while stack:
                    o = stack.pop()
                    if isinstance(o, dict):
                        if isinstance(o.get("type"), int) and "tlBegin" in o:
                            types[o["type"]] += 1
                        stack.extend(o.values())
                    elif isinstance(o, list):
                        stack.extend(o)
    except (OSError, ValueError, zipfile.BadZipFile):
        pass
    return dict(types)


def _hex(color: int) -> str:
    """wfp が持っている縁色を #RRGGBB にする。"""
    return f"#{color & 0xFFFFFF:06X}"


def guess_mapping(speakers: list[wfp.Speaker], known: list[dict]) -> dict[str, str]:
    """wfp 側の話者に、このセットの話者名を当てる下書きを作る。

    当てる順は 3 つ。

      1. プリセット名（「テロップ(タケ)」の タケ）が既存の話者名と一致する
      2. 縁色が settings.json の話者色と一致する
      3. 残ったものを、件数の多い順に空いている話者へ

    3 は当て推量なので、画面で直す前提。ここで決め打ちにしないために、
    どの根拠で当てたかも返す。
    """
    names = [k["name"] for k in known if k.get("name")]
    by_color = {str(k.get("color") or "").upper(): k["name"] for k in known}
    used: set[str] = set()
    out: dict[str, str] = {}
    why: dict[str, str] = {}

    for sp in speakers:
        if sp.preset and sp.preset in names and sp.preset not in used:
            out[sp.label] = sp.preset
            why[sp.label] = "プリセット名"
            used.add(sp.preset)

    for sp in speakers:
        if sp.label in out:
            continue
        hit = by_color.get(_hex(sp.color))
        if hit and hit not in used:
            out[sp.label] = hit
            why[sp.label] = "縁色"
            used.add(hit)

    rest = [sp for sp in sorted(speakers, key=lambda s: -s.count) if sp.label not in out]
    free = [n for n in names if n not in used]
    for sp, name in zip(rest, free):
        out[sp.label] = name
        why[sp.label] = "件数の多い順（要確認）"
        used.add(name)

    return {"mapping": out, "why": why}


def preview(paths: SetPaths, name: str) -> dict:
    """取り込む前に見せる内容。ここでは何も書かない。"""
    target = _resolve(paths, name)
    try:
        res = wfp.extract(target)
    except wfp.WfpError as e:
        raise ImportError_(str(e)) from e

    known = project.load_settings(paths)
    guess = guess_mapping(res.speakers, known)
    cur = project.read_origin(paths.project_file)
    now = cur.get("count") or 0

    warnings = list(res.warnings)

    # 0 件のときに「0 件」とだけ出しても、なぜかが分からない。中身を数えて理由を出す。
    # 実際、字幕を入れる前のプロジェクトを指したときに「取り込む件数 0 件」としか
    # 出ず、ファイル違いなのか不具合なのかが分からなかった。
    if not res.cues:
        n = _count_clips(target)
        warnings.append(
            f"この .wfp にテロップがほとんど入っていません"
            f"（テロップ {n.get(7, 0)} 個 / 映像 {n.get(1, 0)} / 音声 {n.get(2, 0)}）。"
            "字幕を入れる前のプロジェクトか、字幕は別のプロジェクトにあるかもしれません。"
            "Filmora で開いて、タイムラインにテロップが並んでいるか確かめてください")

    if now and len(res.cues) < now * SHRINK_RATIO:
        warnings.append(
            f"いまの字幕 {now} 件に対して、抽出できたのは {len(res.cues)} 件です。"
            "字幕トラックの判定から外れているものがあるかもしれません")

    return {
        "file": target.name,
        "projectName": res.project_name,
        "duration": round(res.duration, 3),
        "count": len(res.cues),
        "currentCount": now,
        "currentOrigin": cur.get("origin"),
        "speakers": [{
            "label": sp.label, "count": sp.count, "first": round(sp.first, 2),
            "color": _hex(sp.color), "preset": sp.preset,
            "assign": guess["mapping"].get(sp.label, ""),
            "why": guess["why"].get(sp.label, ""),
        } for sp in res.speakers],
        "known": [k["name"] for k in known],
        # 字幕トラック外のテロップ。黙って捨てないので中身も見せる
        "excluded": [{"start": round(t.start, 2), "text": t.text[:40]}
                     for t in res.excluded[:20]],
        "excludedCount": len(res.excluded),
        "warnings": warnings,
        # 取り込むとどうなるか
        "willKeepManual": (not paths.manual_file.exists()
                           and cur.get("origin") != project.ORIGIN_WFP),
    }


def run_import(paths: SetPaths, name: str, mapping: dict) -> dict:
    """取り込む。手作業版を退避してから edit.json を差し替える。"""
    target = _resolve(paths, name)
    try:
        res = wfp.extract(target)
    except wfp.WfpError as e:
        raise ImportError_(str(e)) from e
    if not res.cues:
        raise ImportError_("取り込める字幕がありませんでした")

    # 画面で直した対応付けを当てる。空欄のものは wfp 側の呼び名のまま残す
    cues = []
    for c in res.cues:
        speaker = str(mapping.get(c.speaker) or c.speaker or project.UNKNOWN).strip()
        cues.append({"start": c.start, "end": c.end,
                     "speaker": speaker or project.UNKNOWN, "text": c.text})

    kept = project.ensure_manual_snapshot(paths)
    out = project.import_segments(paths, cues,
                                  origin=project.ORIGIN_WFP, keep_as="wfp")
    return {
        **out,
        "file": target.name,
        "keptManual": bool(kept.get("ok")),
        "keptReason": kept.get("reason", ""),
        "at": time.time(),
        "excludedCount": len(res.excluded),
        "warnings": res.warnings,
    }
