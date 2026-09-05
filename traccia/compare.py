"""2 つの版を突き合わせて、どこがどう違うかを分類する。

TRAC-21 で、性質のちがう版が残るようになった。

    auto   … 文字起こしの生出力
    manual … 人が直し切った確定版
    wfp    … Filmora から取り戻した最終版

その差が「文字起こしの弱いところ」を教えてくれる。ただし差の全部が学習の材料に
なるわけではない。**句読点を落とした・相槌を削ったのは演出上の書き直し**であって、
文字起こしの誤りではない。それを混ぜて学習させると、文字起こしの出力が最初から
テロップ寄りになってしまう。だから分類して、何を材料にするかを分ける。

対応付けは diag.align()（TRAC-25）をそのまま使う。時刻を根拠にしないので、
時刻が 40 秒ずれていても本文だけで正しく結べることは実証済み。

区切りの違い（1 対 N / N 対 1）は 1 対 1 の対応付けでは取れないので、後段で拾う。
対応の取れなかった連なりについて、片側を連結すると相手の 1 行になるかを見る。
実測では、人はモデルの 1.27 倍に行を割っていた。ここが差分の最大の塊になる。
"""

from __future__ import annotations

import json
import re
import statistics as st
from dataclasses import dataclass, field
from pathlib import Path

from . import diag

# 本文は同じでも、開始がこれ以上ちがえば「時刻」として数える
TIME_SEC = 0.5

# 時刻の外れ値。要判断へ回す
TIME_OUTLIER = 2.0

# 連結して 1 行になるかを見るとき、この本数まで試す
MERGE_SPAN = 4

# 分類。value は「既定で学習の材料にするか」
KINDS = {
    "一致":     {"learn": False, "note": "そのまま使えた"},
    "時刻":     {"learn": True,  "note": "本文は同じで、置いた位置がちがう"},
    "話者":     {"learn": True,  "note": "話者の割り当てがちがう"},
    "表記ゆれ": {"learn": True,  "note": "かな / 漢字・送り仮名のちがい"},
    "語の誤り": {"learn": True,  "note": "語が 1〜2 か所ちがう"},
    "区切り":   {"learn": True,  "note": "1 行の分け方がちがう"},
    "句読点":   {"learn": False, "note": "句読点・記号だけの差（演出）"},
    "削り":     {"learn": False, "note": "相槌・言い直しを削った（演出）"},
    "書き換え": {"learn": False, "note": "大きく違う。機械では判定できない"},
    "欠落":     {"learn": True,  "note": "右にあって左に無い"},
    "余剰":     {"learn": True,  "note": "左にあって右に無い"},
}

# 要判断へ回すもの。機械が決めきれない
NEEDS_EYE = ("書き換え", "話者")

_PUNCT_ONLY = re.compile(r"^[、。．，・…！？!?～ー\s]*$")
_KANA = re.compile(r"[ぁ-んァ-ヶ]")


@dataclass
class Row:
    kind: str
    left: dict | None = None          # 比べる側（機械の出力など）
    right: dict | None = None         # 正とする側（人が直した版）
    leftIds: list = field(default_factory=list)    # 区切りのときの束
    rightIds: list = field(default_factory=list)
    dStart: float | None = None
    dEnd: float | None = None
    sim: float = 0.0
    at: float = 0.0                   # 並べ替えに使う時刻

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "left": self.left, "right": self.right,
            "leftIds": self.leftIds, "rightIds": self.rightIds,
            "dStart": None if self.dStart is None else round(self.dStart, 2),
            "dEnd": None if self.dEnd is None else round(self.dEnd, 2),
            "sim": round(self.sim, 3), "at": round(self.at, 2),
            "learn": KINDS.get(self.kind, {}).get("learn", False),
            "eye": self.kind in NEEDS_EYE,
        }


def _norm(s: str) -> str:
    return diag.normalize(s)


def _strip_punct(s: str) -> str:
    return re.sub(r"[、。．，・…！？!?\s]", "", str(s or ""))


def classify(a: str, b: str, d_start: float, speaker_diff: bool) -> str:
    """1 対 1 で結ばれた 2 行の、違いの種類を決める。

    見る順に意味がある。**演出上の書き直しを先に外す**のが要点で、
    そうしないと句読点の差が「語の誤り」に紛れ込む。
    """
    na, nb = _norm(a), _norm(b)
    if na == nb:
        if speaker_diff:
            return "話者"
        return "時刻" if abs(d_start) >= TIME_SEC else "一致"

    # 句読点・記号だけの差
    if _strip_punct(a) == _strip_punct(b):
        return "句読点"

    # 片方がもう片方を含む＝削った（相槌・言い直し・言い切りの省略）
    if na in nb or nb in na:
        return "削り"

    r = diag.ratio(a, b)
    if r >= 0.85:
        # かな⇄漢字・送り仮名の違いは、字は違うが読みは同じことが多い。
        # ここを「語の誤り」にすると、学習の材料が表記の揺れで埋まる。
        if _KANA.search(a) or _KANA.search(b):
            return "表記ゆれ"
        return "語の誤り"
    if r >= 0.6:
        return "語の誤り"
    return "書き換え"


def _find_splits(left: list[dict], right: list[dict],
                 pairs: list[tuple[int | None, int | None]]) -> dict:
    """分割・結合を拾う。

    1 対 1 の対応付けでは「モデルの 1 行 = 人の 3 行」が取れない。取れたところが
    1 行ぶんしか結ばれず、残りが「欠落」として散らばる。実測ではここが最大の塊で、
    欠落 174 件・余剰 353 件のほとんどが区切りの違いだった。

    そこで、**結ばれた組の似かたが中途半端なとき**に、隣の未対応の行を足したら
    似かたが上がるかを見る。上がるなら、それは 1 行を割った（あるいはまとめた）
    ものとして 1 つの行に括る。
    """
    matched = [(li, ri) for li, ri in pairs if li is not None and ri is not None]
    free_l = {li for li, ri in pairs if ri is None and li is not None}
    free_r = {ri for li, ri in pairs if li is None and ri is not None}

    out: dict[int, tuple[list[int], list[int]]] = {}
    used_l: set[int] = set()
    used_r: set[int] = set()

    for li, ri in matched:
        if li in used_l or ri in used_r:
            continue
        base = diag.ratio(left[li]["text"], right[ri]["text"])
        if base >= 0.95:
            continue                      # すでにぴたりと合っている

        best = (base, [li], [ri])
        # 右（人の側）を足していく＝モデルが 1 行にまとめたものを人が割った
        acc = right[ri]["text"]
        rs = [ri]
        for k in range(1, MERGE_SPAN):
            nxt = ri + k
            if nxt not in free_r or nxt in used_r:
                break
            acc += right[nxt]["text"]
            rs = rs + [nxt]
            r = diag.ratio(left[li]["text"], acc)
            if r > best[0] + 0.05:
                best = (r, [li], list(rs))
        # 左を足していく＝モデルが割ったものを人が 1 行にまとめた
        acc = left[li]["text"]
        ls = [li]
        for k in range(1, MERGE_SPAN):
            nxt = li + k
            if nxt not in free_l or nxt in used_l:
                break
            acc += left[nxt]["text"]
            ls = ls + [nxt]
            r = diag.ratio(acc, right[ri]["text"])
            if r > best[0] + 0.05:
                best = (r, list(ls), [ri])

        r, ls, rs = best
        if len(ls) == 1 and len(rs) == 1:
            continue                      # 束ねる価値が無かった
        out[ls[0]] = (ls, rs)
        used_l.update(ls)
        used_r.update(rs)
    return out


# 全体がこれ以上ずれていたら、素材の基準が違うとみなして 1 回だけ報告する
GLOBAL_SHIFT = 1.0


def _global_shift(left: list[dict], right: list[dict],
                  pairs: list[tuple[int | None, int | None]]) -> float:
    """左右で時刻の基準そのものがずれていないか。

    Filmora で頭を詰めると、wfp から取った版は全体が前へ寄る。実測では
    −26.6 秒ずれていて、そのままだと 709 行すべてが「時刻がちがう」になり、
    本当に見たい差が埋もれた。**全体のずれは 1 回だけ報告して、残差で分類する。**
    """
    ds = [left[li]["start"] - right[ri]["start"]
          for li, ri in pairs if li is not None and ri is not None
          and diag.ratio(left[li]["text"], right[ri]["text"]) >= 0.8]
    if len(ds) < 10:
        return 0.0
    m = st.median(ds)
    return m if abs(m) >= GLOBAL_SHIFT else 0.0


def build(left: list[dict], right: list[dict],
          left_name: str = "left", right_name: str = "right") -> dict:
    """左（比べる側）と右（正とする側）を突き合わせる。"""
    pairs = diag.align(left, right)
    shift = _global_shift(left, right, pairs)
    splits = _find_splits(left, right, pairs)
    in_split_l = {i for ls, _ in splits.values() for i in ls}
    in_split_r = {i for _, rs in splits.values() for i in rs}

    def cue(seg: dict, idx: int) -> dict:
        return {"i": idx + 1, "start": round(seg["start"], 2),
                "end": round(seg["end"], 2),
                "speaker": seg.get("speaker") or "", "text": seg.get("text") or ""}

    rows: list[Row] = []
    for li, ri in pairs:
        if li is not None and li in in_split_l:
            if li in splits:
                ls, rs = splits[li]
                rows.append(Row(
                    kind="区切り",
                    left=cue(left[ls[0]], ls[0]), right=cue(right[rs[0]], rs[0]),
                    leftIds=[cue(left[i], i) for i in ls],
                    rightIds=[cue(right[i], i) for i in rs],
                    dStart=left[ls[0]]["start"] - right[rs[0]]["start"] - shift,
                    sim=diag.ratio("".join(left[i]["text"] for i in ls),
                                   "".join(right[i]["text"] for i in rs)),
                    at=right[rs[0]]["start"]))
            continue
        if ri is not None and ri in in_split_r:
            continue

        if li is None:
            rows.append(Row(kind="欠落", right=cue(right[ri], ri),
                            at=right[ri]["start"]))
            continue
        if ri is None:
            rows.append(Row(kind="余剰", left=cue(left[li], li),
                            at=left[li]["start"]))
            continue

        l, r = left[li], right[ri]
        sim = diag.ratio(l["text"], r["text"])
        if sim < diag.MATCH_MIN:
            # 結ばれたが似ていない。無理に 1 行として扱わず、別々に見せる
            rows.append(Row(kind="余剰", left=cue(l, li), at=l["start"]))
            rows.append(Row(kind="欠落", right=cue(r, ri), at=r["start"]))
            continue
        d = l["start"] - r["start"] - shift
        spk = (l.get("speaker") or "") != (r.get("speaker") or "")
        rows.append(Row(kind=classify(l["text"], r["text"], d, spk),
                        left=cue(l, li), right=cue(r, ri),
                        dStart=d, dEnd=l["end"] - r["end"] - shift, sim=sim,
                        at=r["start"]))

    rows.sort(key=lambda x: x.at)

    counts: dict[str, int] = {}
    for x in rows:
        counts[x.kind] = counts.get(x.kind, 0) + 1
    ds = [x.dStart for x in rows if x.dStart is not None]

    return {
        "left": {"name": left_name, "count": len(left)},
        "right": {"name": right_name, "count": len(right)},
        "rows": [x.to_dict() for x in rows],
        "kinds": {k: {**v, "count": counts.get(k, 0)} for k, v in KINDS.items()},
        "summary": {
            "shift": round(shift, 2),
            "rows": len(rows),
            "same": counts.get("一致", 0),
            "sameRate": round(100 * counts.get("一致", 0) / max(1, len(rows))),
            "eye": sum(1 for x in rows if x.kind in NEEDS_EYE),
            "learn": sum(1 for x in rows if KINDS.get(x.kind, {}).get("learn")),
            "dStartMedian": round(st.median(ds), 2) if ds else None,
            "dStartP10": round(sorted(ds)[int(len(ds) * .1)], 2) if ds else None,
            "dStartP90": round(sorted(ds)[int(len(ds) * .9)], 2) if ds else None,
            "outliers": sum(1 for d in ds if abs(d) >= TIME_OUTLIER),
        },
    }


def save(path: Path, doc: dict) -> None:
    """結果を残す。TRAC-24 がこれを読む。"""
    slim = {**doc, "rows": [r for r in doc["rows"] if r["kind"] != "一致"]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
