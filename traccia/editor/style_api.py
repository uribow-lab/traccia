"""作法（字幕の作り方）の確認と反映。

測るのも提案を作るのも traccia/style.py の仕事。ここはファイルの置き場所を決めて、
画面に返す形に整えるだけ。
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import style as style_mod
from . import project
from .project import SetPaths


def _style_file(paths: SetPaths) -> Path:
    return paths.root / f"{paths.stem}.style.json"


def _read_feedback(f: Path) -> dict | None:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _feedback(paths: SetPaths) -> dict | None:
    """比較画面（TRAC-23）が残した、このセットの差分。"""
    f = paths.root / f"{paths.stem}.feedback.json"
    return _read_feedback(f) if f.exists() else None


def _all_feedback(resources: Path) -> list[dict]:
    """素材フォルダ全体の差分。語は素材をまたいで数えないと繰り返しが出ない。"""
    out = []
    for d in sorted(p for p in resources.iterdir() if p.is_dir()):
        for f in d.glob("*.feedback.json"):
            fb = _read_feedback(f)
            if fb:
                out.append(fb)
    return out


def overview(paths: SetPaths, resources: Path) -> dict:
    """いま使われる作法・測った作法・提案。"""
    applied = style_mod.load_applied(_style_file(paths))
    measured = style_mod.for_set(paths.root, paths.stem, resources)
    current = style_mod.effective(paths.root, paths.stem, resources)
    vocab = project.load_vocab(paths)
    fb = _feedback(paths)

    return {
        "current": current.to_dict(),
        "measured": measured.to_dict(),
        "applied": bool(applied.get("style")),
        "appliedAt": applied.get("at"),
        "canUndo": bool(applied.get("history")),
        "promptLines": style_mod.prompt_lines(current),
        "suggestions": style_mod.suggest(current, measured, fb, vocab["terms"],
                                         feedbacks=_all_feedback(resources)),
        "hasFeedback": bool(fb),
        "terms": vocab["terms"],
    }


def act(paths: SetPaths, resources: Path, what: str, body: dict) -> dict:
    """提案を反映する / 取り消す。

    反映したものは <名前>.style.json に残し、前の値を履歴に積む。押した結果が
    取り返しのつかないものであってはいけないので、取り消しは必ず用意する。
    """
    f = _style_file(paths)
    if what == "undo":
        style_mod.undo(f)
        return overview(paths, resources)

    ids = set(body.get("ids") or [])
    if not ids:
        return overview(paths, resources)

    if "style" in ids:
        measured = style_mod.for_set(paths.root, paths.stem, resources)
        style_mod.apply(f, measured, note=f"{measured.from_} から測定")

    # 語は既存の terms の仕組みにそのまま乗せる
    terms = [t for i in ids if i.startswith("term:") for t in [i[5:]]]
    if terms:
        vocab = project.load_vocab(paths)
        merged = list(vocab["terms"])
        for t in terms:
            if t not in merged:
                merged.append(t)
        project.save_settings(paths, {"terms": merged})

    return overview(paths, resources)
