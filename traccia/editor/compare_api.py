"""比較画面のためのサーバー側。

どの版を読むかを決めて、traccia/compare.py に渡すだけ。分類そのものは
compare.py が持っている。
"""

from __future__ import annotations

from .. import compare, diag
from .project import SetPaths
from . import project

# 画面に出す選択肢。current はいま編集している edit.json
CHOICES = [
    {"id": "current", "label": "いま編集中"},
    {"id": "auto",    "label": "文字起こしの生出力"},
    {"id": "manual",  "label": "手作業の確定版"},
    {"id": "wfp",     "label": "wfp の最終版"},
]


class CompareError(Exception):
    """呼び出し側に見せるエラー。文面はそのまま画面に出る。"""


def _load(paths: SetPaths, kind: str) -> list[dict]:
    f = paths.project_file if kind == "current" else paths.generation_file(kind)
    if not f.exists():
        label = next((c["label"] for c in CHOICES if c["id"] == kind), kind)
        raise CompareError(f"「{label}」がまだありません（{f.name}）")
    return diag.load_truth(f)


def available(paths: SetPaths) -> list[dict]:
    out = []
    for c in CHOICES:
        f = (paths.project_file if c["id"] == "current"
             else paths.generation_file(c["id"]))
        info = project.read_origin(f) if f.exists() else {}
        out.append({**c, "exists": f.exists(), "count": info.get("count")})
    return out


def run(paths: SetPaths, left: str, right: str) -> dict:
    ids = {c["id"] for c in CHOICES}
    if left not in ids or right not in ids:
        raise CompareError("比べる版の指定が正しくありません")
    if left == right:
        raise CompareError("同じ版どうしは比べられません")

    l, r = _load(paths, left), _load(paths, right)
    label = {c["id"]: c["label"] for c in CHOICES}
    doc = compare.build(l, r, label[left], label[right])
    doc["set"] = paths.name
    doc["choices"] = available(paths)
    doc["leftId"], doc["rightId"] = left, right
    doc["mediaUrl"] = f"/media/{paths.name}"
    try:
        compare.save(paths.root / f"{paths.stem}.feedback.json", doc)
    except OSError:
        pass
    return doc
