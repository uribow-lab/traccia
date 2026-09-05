"""人が直し終えた字幕から「この現場の作法」を測り、文字起こしに渡す。

なぜ要るか
----------
プロンプトは長らく「1 区間は 1〜5 秒程度」と指示していた。ところが実際に人が
作る字幕は中央 1.43 秒だった。指示が実態と合っていない。

実測に合わせて流し直したら、拾えた行が 67% → 75%、欠落が 350 → 174 件になった。
総字数はほぼ変わらない（7,953 → 8,041 字。確定版は 7,833 字）ので、内容が
増えたのではなく**同じ内容を細かく割っただけ**。時刻の精度は動かない。

    これまで      570 行 / 1 行 2.20 秒 / 拾えた行 67%
    実測に合わせた 913 行 / 1 行 1.50 秒 / 拾えた行 75%
    確定版（人）   734 行 / 1 行 1.43 秒

つまり **プロンプトの数字を実測に寄せるだけで効く**。ここはそれを自動でやる。

1 つの固定値では足りない
------------------------
手元の確定版 5 本を測ると、素材ごとに 0.7 秒の幅がある。

    bluebottle 1.27s 9字 / ooseyana 1.26s 9字 / spring_valley 1.44s 10字
    yellow_company 1.43s 10字 / princi 1.94s 12字

だからセットごとに測る。確定版がまだ無いセットは、同じ素材フォルダの他のセットの
中央値へ落とし、それも無ければ既定値で流す（従来どおり動かすため）。

公開して他の人が使うことも考える。1.5 秒はこの現場の作法であって普遍の正解ではない。
その人の確定版から測る作りにしておけば、誰が使ってもその人の作法が学習される。
"""

from __future__ import annotations

import json
import re
import statistics as st
import time
from dataclasses import dataclass, asdict
from pathlib import Path

# 確定版が無いときの出発点。gemini.py のプロンプトに書いている値と揃える。
DEFAULT = {"sec": 1.5, "chars": 10, "maxSec": 3.0, "maxChars": 20}

# これ未満の行数しか無い確定版は、作法として信用しない
MIN_CUES = 60

# フィラー。残す割合を測って、プロンプトに書くかどうかを決める
FILLERS = ("うん", "うーん", "まあ", "まぁ", "あの", "あのー", "その", "えー",
           "なんか", "そうそう", "はい")


@dataclass
class Style:
    sec: float = DEFAULT["sec"]           # 1 行の尺（中央値）
    chars: int = DEFAULT["chars"]         # 1 行の文字数（中央値）
    maxSec: float = DEFAULT["maxSec"]     # 9 割目の尺（ここまでは許す）
    maxChars: int = DEFAULT["maxChars"]
    fillerRate: float = 0.0               # フィラーで始まる行の割合
    cues: int = 0                         # 測った元の行数
    source: str = "default"               # set / folder / default
    from_: str = ""                       # どのファイルから測ったか

    def to_dict(self) -> dict:
        d = asdict(self)
        d["from"] = d.pop("from_")
        return d


def measure(cues: list[dict]) -> Style | None:
    """字幕の並びから作法を測る。行数が少なすぎるときは None。"""
    rows = [c for c in cues if str(c.get("text") or "").strip()]
    if len(rows) < MIN_CUES:
        return None
    dur = sorted(float(c["end"]) - float(c["start"]) for c in rows)
    ch = sorted(len(str(c["text"]).strip()) for c in rows)
    p90 = lambda xs: xs[int(len(xs) * 0.9)]  # noqa: E731
    heads = sum(1 for c in rows
                if str(c["text"]).lstrip().startswith(FILLERS))
    return Style(
        sec=round(st.median(dur), 2),
        chars=int(st.median(ch)),
        maxSec=round(p90(dur), 1),
        maxChars=int(p90(ch)),
        fillerRate=round(heads / len(rows), 3),
        cues=len(rows),
    )


def _cues_of(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("cues") or []
    except (OSError, ValueError):
        return []


def _confirmed_file(root: Path, stem: str) -> Path | None:
    """そのセットで「人が直し終えた版」として信用できるファイル。

    手作業の確定版があればそれ。無ければ現役の編集データ。ただし現役のほうは
    文字起こし直後（origin=transcribe）だと機械の出力そのものなので使わない。
    機械の出力から作法を測ると、いまの癖をそのまま学び直すことになる。
    """
    manual = root / f"{stem}.manual.edit.json"
    if manual.exists():
        return manual
    cur = root / f"{stem}.edit.json"
    if not cur.exists():
        return None
    try:
        origin = json.loads(cur.read_text(encoding="utf-8")).get("origin")
    except (OSError, ValueError):
        return None
    return None if origin == "transcribe" else cur


def for_set(root: Path, stem: str, resources: Path | None = None) -> Style:
    """このセットに使う作法を決める。セット → 同じ場所の他セット → 既定値。"""
    f = _confirmed_file(root, stem)
    if f:
        s = measure(_cues_of(f))
        if s:
            s.source, s.from_ = "set", f.name
            return s

    # 同じ素材フォルダの他のセットから。現場の作法は素材をまたいで似ている
    base = resources or root.parent
    got: list[Style] = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        if d == root:
            continue
        for cand in d.glob("*.edit.json"):
            if any(k in cand.name for k in (".auto.", ".wfp.")):
                continue
            s = measure(_cues_of(cand))
            if s:
                got.append(s)
            break
    if got:
        out = Style(
            sec=round(st.median([g.sec for g in got]), 2),
            chars=int(st.median([g.chars for g in got])),
            maxSec=round(st.median([g.maxSec for g in got]), 1),
            maxChars=int(st.median([g.maxChars for g in got])),
            fillerRate=round(st.median([g.fillerRate for g in got]), 3),
            cues=sum(g.cues for g in got),
            source="folder", from_=f"{len(got)} 本のセットから",
        )
        return out

    return Style(source="default", from_="既定値")


def prompt_lines(s: Style) -> str:
    """プロンプトに差し込む文。gemini.py の build_prompt が使う。"""
    out = (f"- 区間は短く刻む。**1 区間 {s.sec:.1f} 秒前後・{s.chars} 文字前後**が目安。"
           f"長くても {s.maxSec:.0f} 秒・{s.maxChars} 文字まで\n")
    if s.fillerRate >= 0.05:
        out += ("- 「うん」「まあ」「あの」のような相槌・つなぎも、聞こえたとおり"
                "そのまま書く（削らない）\n")
    else:
        out += "- 「うん」「まあ」のようなつなぎは、意味が変わらなければ省いてよい\n"
    return out


# ---------------------------------------------------------------- 提案

def term_counts(feedbacks: list[dict]) -> dict[tuple[str, str], int]:
    """複数の差分から、入れ替わっている語を数える。

    1 本の素材では同じ誤りが 2 回出ることはほとんど無い（実測 145 件のうち
    2 回以上は 0 種）。**現場をまたいで数えて初めて「いつも間違える語」が浮かぶ。**
    """
    counts: dict[tuple[str, str], int] = {}
    for fb in feedbacks:
        for r in fb.get("rows") or []:
            if r.get("kind") != "語の誤り" or not (r.get("left") and r.get("right")):
                continue
            w = _diff_word(r["left"]["text"], r["right"]["text"])
            if w:
                counts[w] = counts.get(w, 0) + 1
    return counts


def suggest(current: Style, measured: Style, feedback: dict | None = None,
            terms: list[str] | None = None,
            feedbacks: list[dict] | None = None) -> list[dict]:
    """人に見せる改善案。根拠を必ず添える。

    全自動では入れない。数字が数本の素材で偶然寄っているだけかもしれないし、
    現場の作法が変わることもある。決めるのは人。
    """
    out: list[dict] = []
    changed = (abs(measured.sec - current.sec) >= 0.1
               or measured.chars != current.chars
               or measured.source != current.source)
    if measured.source != "default" and changed:
        out.append({
            "id": "style",
            "title": f"1 行の長さを {current.sec:.1f} 秒 → {measured.sec:.1f} 秒 "
                     f"／ {current.chars} 文字 → {measured.chars} 文字",
            "why": f"{measured.from_} の {measured.cues} 行から測定",
            "detail": "文字起こしのプロンプトに入れる目安が変わります。"
                      "実測に合わせると、人の区切りと噛み合う数が増えます",
            "kind": "style",
        })

    # 繰り返し間違えている語。効く範囲は限定的だが、既存の terms に乗せるだけで安い。
    # 素材をまたいで数える（1 本では 2 回以上の繰り返しがほぼ出ない）。
    pool = list(feedbacks or ([feedback] if feedback else []))
    if pool:
        counts = term_counts(pool)
        known = set(terms or [])
        # 素材が 1 本しか無いうちは、繰り返しを待てない。1 回でも候補に出し、
        # 「1 回だけ」と根拠に書く。判断するのは人。
        least = 2 if len(pool) > 1 else 1
        for (wrong, right), n in sorted(counts.items(), key=lambda x: -x[1]):
            if n < least or right in known or len(right) < 2:
                continue
            out.append({
                "id": f"term:{right}",
                "title": f"「{right}」を固有名詞に足す",
                "why": (f"「{wrong}」と {n} 回まちがえていた"
                        + ("" if n > 1 else "（素材 1 本だけの観測）")),
                "detail": "次の文字起こしで、この表記で書かれやすくなります",
                "kind": "term", "term": right,
            })
            if len([x for x in out if x["kind"] == "term"]) >= 12:
                break
    return out


# 固有名詞の候補として認める形。助詞や語尾の揺れを拾っても辞書には使えない。
_KATAKANA = re.compile(r"^[ァ-ヶー]{3,12}$")
_KANJI_WORD = re.compile(r"^[一-龠]{2,8}$")
_ALNUM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{1,15}$")


def _diff_word(a: str, b: str) -> tuple[str, str] | None:
    """2 つの本文で、入れ替わっている部分を取り出す。

    日本語には語の区切りが無いので、正規表現で「語」に割ろうとすると
    文まるごとが 1 語になる（実際そうなって、提案が 1 件も出なかった）。
    前後の共通部分を削って、残った真ん中だけを見る。

        「スープがないじゃんね」 と 「スープじゃないじゃんね」
          共通の頭「スープ」、共通の尻「ないじゃんね」を削ると
          → 「が」 と 「じゃ」

    そのうえで、短すぎるもの・かなだけのものは捨てる。固有名詞の候補として
    使いたいので、助詞や語尾の揺れを拾っても意味がない。
    """
    a, b = a.strip(), b.strip()
    if not a or not b or a == b:
        return None
    head = 0
    while head < len(a) and head < len(b) and a[head] == b[head]:
        head += 1
    tail = 0
    while (tail < len(a) - head and tail < len(b) - head
           and a[len(a) - 1 - tail] == b[len(b) - 1 - tail]):
        tail += 1
    wrong, right = a[head:len(a) - tail], b[head:len(b) - tail]
    if not wrong or not right:
        return None
    # 固有名詞の候補になりうるものだけを残す。
    #
    # 実データで測ったら、取り出せた 44 種のうち固有名詞らしいものは 2 種だけで、
    # 残りは助詞や語尾の揺れだった（「看板の的な味」「よ 全面に」など）。
    # 辞書に入れても効かないどころか、プロンプトを汚す。カタカナ主体・漢字語・
    # 英数字だけに絞る。
    if not (_KATAKANA.match(right) or _KANJI_WORD.match(right)
            or _ALNUM.match(right)):
        return None
    return (wrong, right)


# ---------------------------------------------------------------- 反映

def load_applied(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def apply(path: Path, style: Style, note: str = "") -> dict:
    """作法を反映する。前の値を履歴に残して、取り消せるようにする。

    1 回目の反映でも履歴を積む。積まないと「初めて反映したあと元に戻せない」
    という状態になり、押すのが怖い機能になる。戻す先が既定値なら、それを
    履歴として残しておけばよい。
    """
    doc = load_applied(path)
    hist = doc.get("history") or []
    prev = doc.get("style") or Style(source="default",
                                     from_="既定値（反映前）").to_dict()
    hist.insert(0, {"style": prev, "at": doc.get("at"),
                    "note": doc.get("note", "反映前の状態")})
    doc = {"version": 1, "style": style.to_dict(), "at": time.time(),
           "note": note, "history": hist[:10]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return doc


def undo(path: Path) -> dict:
    """1 つ前に戻す。押した結果が取り返しのつかないものであってはいけない。

    履歴が空になったら、反映していない状態（既定値で動く）に戻す。
    """
    doc = load_applied(path)
    hist = doc.get("history") or []
    if not hist:
        return doc
    prev = hist.pop(0)
    if str(prev.get("style", {}).get("source")) == "default" and not hist:
        # 反映前の状態へ戻る＝ファイルごと消して、既定値で動く形に戻す
        try:
            path.unlink()
        except OSError:
            pass
        return {}
    doc = {"version": 1, "style": prev["style"], "at": time.time(),
           "note": "取り消して戻した", "history": hist}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return doc


def effective(root: Path, stem: str, resources: Path | None = None) -> Style:
    """実際に文字起こしへ渡す作法。

    **承認したものだけを使う。** 測っただけの値は使わない。人の確定版から取った
    数字とはいえ、素材が変われば作法も変わる（手元の 5 本で 1.26〜1.94 秒の幅が
    あった）。黙って変わると、なぜ出力が変わったのかが分からなくなる。
    """
    saved = load_applied(root / f"{stem}.style.json")
    if saved.get("style"):
        d = dict(saved["style"])
        d["from_"] = d.pop("from", "")
        try:
            return Style(**d)
        except TypeError:
            pass
    return Style(source="default", from_="既定値。まだ反映していません")
