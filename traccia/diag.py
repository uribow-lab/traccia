"""文字起こしの時刻のずれを測る。

    python -m traccia diag resources/yellow_company_verification --chunk 240

Gemini が返した生の時刻を <名前>.gemini-raw.json に残し（書くのは
traccia/gemini.py の _write_diag）、人が直し終えた確定版（edit.json）を
正解として突き合わせる。edit.json には一切書かない。

なぜ要るか
----------
文字は概ね合っているのに、出現位置が 3〜5 秒ずれる箇所が 1 本に 4〜5 か所ある。
機構の候補は 2 つあって、直し方が変わる。

  (a) チャンク内のドリフト … 長い無音や重なりでモデルが同期を失い、
      そのチャンクの以降が一律ずれる。次のチャンクは offset が厳密なので戻る
  (b) 重なった発話の後置き … 他の人の発話中に挟まれた相槌を、その発話の
      後ろに並べて時刻を振る。必ず後ろへずれ、話者ごとに偏る

(a) ならチャンク長の短縮が効き、(b) なら効かない。ずれの箇所数がチャンク数に
比例して増えるか、ずれた箇所の直前が重なりだったか、で見分ける。

対応付けの作り方
----------------
「いちばん似ている確定版の行」を素朴に選ぶと、「うん」「はい」のような短い
相槌が遠くの同じ文言に付いてしまい、ずれを測っているのか対応付けを間違えて
いるのか分からなくなる。そこで**順序を保った整合**（編集距離と同じ形の DP）で
対応付ける。会話は前から順に進むので、この制約は正しく効く。
"""

from __future__ import annotations

import json
import math
import re
import statistics as st
from dataclasses import dataclass, field
from pathlib import Path

# これ以上ずれていたら「ずれ」として数える（0.n 秒は許容範囲）
SHIFT_SEC = 2.0

# 隣り合うずれがこの差の内なら、同じ 1 か所のずれとしてまとめる
CLUSTER_TOL = 1.5

# ずれの塊として数えるのに要る最低の行数（単発の外れ値と区別する）
CLUSTER_MIN = 2

# 直前がこれ以上空いていたら「長い無音のあと」とみなす
SILENCE_SEC = 2.0

# 対応付けを認める文言の似かたの下限
MATCH_MIN = 0.55

_PUNCT = re.compile(r"[、。．，\s?？!！「」『』…・ー]")


def normalize(text: str) -> str:
    """比べるための形にする。句読点と空白は落とす。"""
    return _PUNCT.sub("", str(text or ""))


def ratio(a: str, b: str) -> float:
    """2 語の似かた。0〜1。

    difflib は短い日本語で当たりが甘いので、2 文字の並び（bigram）の
    重なりを使う。1 文字だと「はい」と「いは」が同じになってしまう。
    """
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) == 1 or len(b) == 1:
        return 1.0 if a == b else 0.0
    ga = [a[i:i + 2] for i in range(len(a) - 1)]
    gb = [b[i:i + 2] for i in range(len(b) - 1)]
    common = 0
    pool = list(gb)
    for g in ga:
        if g in pool:
            pool.remove(g)
            common += 1
    return 2 * common / (len(ga) + len(gb))


# ---------------------------------------------------------------- 対応付け

def align(hyp: list[dict], truth: list[dict]) -> list[tuple[int | None, int | None]]:
    """順序を保ったまま対応付ける。

    戻り値は (hyp の添字, truth の添字) の並び。片方が None なら
    余った側（余剰 / 欠落）。
    """
    n, m = len(hyp), len(truth)
    if not n or not m:
        return [(i, None) for i in range(n)] + [(None, j) for j in range(m)]

    # 似かたを先に測っておく。時刻は使わない（時刻を信じないための測定なので、
    # 時刻を対応付けの根拠にすると自分の尻尾を追うことになる）。
    sim = [[ratio(h["text"], t["text"]) for t in truth] for h in hyp]

    GAP = -0.35            # 対応させずに飛ばすときの重み
    NEG = -1e9

    best = [[NEG] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]
    best[0][0] = 0.0
    for i in range(1, n + 1):
        best[i][0] = best[i - 1][0] + GAP
        back[i][0] = 1
    for j in range(1, m + 1):
        best[0][j] = best[0][j - 1] + GAP
        back[0][j] = 2

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            s = sim[i - 1][j - 1]
            # 似ていない組を無理に結ばない。結ぶ価値は 0.5 を境に符号が変わる
            diag = best[i - 1][j - 1] + (s - 0.5)
            up = best[i - 1][j] + GAP
            left = best[i][j - 1] + GAP
            if diag >= up and diag >= left:
                best[i][j], back[i][j] = diag, 0
            elif up >= left:
                best[i][j], back[i][j] = up, 1
            else:
                best[i][j], back[i][j] = left, 2

    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        move = back[i][j] if (i > 0 and j > 0) else (1 if i > 0 else 2)
        if move == 0:
            i, j = i - 1, j - 1
            pairs.append((i, j))
        elif move == 1:
            i -= 1
            pairs.append((i, None))
        else:
            j -= 1
            pairs.append((None, j))
    pairs.reverse()
    return pairs


# ---------------------------------------------------------------- 集計

@dataclass
class Pair:
    hyp: dict
    truth: dict
    sim: float
    dStart: float
    dEnd: float
    chunk: int | None = None
    toBoundary: float | None = None
    afterSilence: bool = False
    afterOverlap: bool = False
    gapPrev: float | None = None      # 直前の行の終わりから、この行の始まりまで（負なら重なり）
    prevSpeaker: str | None = None


@dataclass
class Cluster:
    """連続して同じ方向に同じ量ずれている範囲。ここを 1 か所と数える。"""
    at: float
    until: float
    count: int
    median: float
    speakers: dict = field(default_factory=dict)
    afterSilence: bool = False
    afterOverlap: bool = False
    gapPrev: float | None = None
    prevSpeaker: str | None = None
    chunk: int | None = None
    toBoundary: float | None = None
    samples: list = field(default_factory=list)


def _chunk_of(t: float, chunk_sec: float, chunks: int) -> int:
    return min(chunks, int(t // chunk_sec) + 1)


def measure(raw: dict, truth_cues: list[dict]) -> dict:
    """生の記録と確定版から、報告に出す数字をすべて作る。"""
    hyp = list(raw.get("segments") or [])
    chunk_sec = float(raw.get("chunkSec") or 0) or 240.0
    chunks = int(raw.get("chunks") or 1)
    duration = float(raw.get("duration") or 0.0)

    pairs: list[Pair] = []
    extra: list[dict] = []      # モデルにあって確定版に無い
    missing: list[dict] = []    # 確定版にあってモデルに無い

    for hi, ti in align(hyp, truth_cues):
        if hi is None:
            missing.append(truth_cues[ti])
            continue
        if ti is None:
            extra.append(hyp[hi])
            continue
        h, t = hyp[hi], truth_cues[ti]
        sim = ratio(h["text"], t["text"])
        if sim < MATCH_MIN:
            # 結ばれたが似ていない。時刻の材料にはしない（本文が別物）
            extra.append(h)
            missing.append(t)
            continue
        p = Pair(hyp=h, truth=t, sim=sim,
                 dStart=h["start"] - t["start"], dEnd=h["end"] - t["end"])
        p.chunk = _chunk_of(t["start"], chunk_sec, chunks)
        edge = min(abs(t["start"] - (p.chunk - 1) * chunk_sec),
                   abs(p.chunk * chunk_sec - t["start"]))
        p.toBoundary = round(edge, 2)
        prev = truth_cues[ti - 1] if ti > 0 else None
        if prev:
            gap = t["start"] - prev["end"]
            p.gapPrev = round(gap, 3)
            p.prevSpeaker = prev["speaker"]
            p.afterSilence = gap >= SILENCE_SEC
            # 「重なり」は音が実際に重なっている場合に限る。話者が違うだけで
            # 重なりとみなすと、ふつうの掛け合いまで拾って判定が緩くなる。
            p.afterOverlap = gap < -0.2 and prev["speaker"] != t["speaker"]
        pairs.append(p)

    ds = [p.dStart for p in pairs]
    shifted = [p for p in pairs if abs(p.dStart) >= SHIFT_SEC]

    # ずれの塊にまとめる。時刻順に見て、同じ方向・近い量のものを束ねる。
    clusters: list[Cluster] = []
    run: list[Pair] = []

    def close_run() -> None:
        if len(run) < CLUSTER_MIN:
            run.clear()
            return
        vals = [p.dStart for p in run]
        spk: dict = {}
        for p in run:
            spk[p.truth["speaker"]] = spk.get(p.truth["speaker"], 0) + 1
        clusters.append(Cluster(
            at=round(run[0].truth["start"], 2),
            until=round(run[-1].truth["end"], 2),
            count=len(run),
            median=round(st.median(vals), 2),
            speakers=spk,
            afterSilence=run[0].afterSilence,
            afterOverlap=run[0].afterOverlap,
            gapPrev=run[0].gapPrev,
            prevSpeaker=run[0].prevSpeaker,
            chunk=run[0].chunk,
            toBoundary=run[0].toBoundary,
            samples=[{"at": round(p.truth["start"], 2),
                      "d": round(p.dStart, 2),
                      "speaker": p.truth["speaker"],
                      "truth": p.truth["text"][:24],
                      "hyp": p.hyp["text"][:24]} for p in run[:6]],
        ))
        run.clear()

    for p in shifted:
        if run and (abs(p.dStart - run[-1].dStart) > CLUSTER_TOL
                    or (p.dStart > 0) != (run[-1].dStart > 0)):
            close_run()
        run.append(p)
    close_run()

    by_speaker: dict = {}
    for p in pairs:
        by_speaker.setdefault(p.truth["speaker"], []).append(p.dStart)

    dropped = [d for c in raw.get("chunkList") or [] for d in (c.get("dropped") or [])]
    by_reason: dict = {}
    for d in dropped:
        by_reason[d["reason"]] = by_reason.get(d["reason"], 0) + 1

    return {
        "duration": round(duration, 1),
        "chunkSec": chunk_sec,
        "chunks": chunks,
        "hypCount": len(hyp),
        "truthCount": len(truth_cues),
        "paired": len(pairs),
        "extra": len(extra),
        "missing": len(missing),
        "start": {
            "median": round(st.median(ds), 3) if ds else None,
            "mean": round(st.fmean(ds), 3) if ds else None,
            "p10": round(_pct(ds, 10), 3) if ds else None,
            "p90": round(_pct(ds, 90), 3) if ds else None,
            "min": round(min(ds), 3) if ds else None,
            "max": round(max(ds), 3) if ds else None,
        },
        "shiftedCount": len(shifted),
        "shiftedLate": sum(1 for p in shifted if p.dStart > 0),
        "shiftedEarly": sum(1 for p in shifted if p.dStart < 0),
        "clusters": [vars(c) for c in clusters],
        "clusterCount": len(clusters),
        "clustersPerChunk": round(len(clusters) / chunks, 2) if chunks else None,
        "clustersPer10min": (round(len(clusters) / (duration / 600), 2)
                             if duration else None),
        "afterSilence": sum(1 for c in clusters if c.afterSilence),
        "afterOverlap": sum(1 for c in clusters if c.afterOverlap),
        "gapPrevMedian": (round(st.median([c.gapPrev for c in clusters
                                           if c.gapPrev is not None]), 3)
                          if any(c.gapPrev is not None for c in clusters) else None),
        "nearBoundary": sum(1 for c in clusters
                            if c.toBoundary is not None and c.toBoundary <= 15),
        "bySpeaker": {k: {"count": len(v), "median": round(st.median(v), 3),
                          "shifted": sum(1 for x in v if abs(x) >= SHIFT_SEC)}
                      for k, v in sorted(by_speaker.items())},
        "dropped": {"total": len(dropped), "byReason": by_reason,
                    "samples": dropped[:12]},
        "merged": {"total": len(raw.get("merged") or []),
                   "samples": (raw.get("merged") or [])[:12]},
        "missingSamples": [{"at": round(t["start"], 2), "speaker": t["speaker"],
                            "text": t["text"][:30]} for t in missing[:12]],
        "extraSamples": [{"at": round(h["start"], 2), "speaker": h["speaker"],
                          "text": h["text"][:30]} for h in extra[:12]],
    }


def _pct(vals: list[float], p: float) -> float:
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


# ---------------------------------------------------------------- 報告

def report(m: dict) -> str:
    """人が読む形。数字だけ並べても見立てにならないので、区分けして出す。"""
    L: list[str] = []
    add = L.append

    add(f"素材 {m['duration']:.0f} 秒 / チャンク {m['chunkSec']:.0f} 秒 × {m['chunks']} 本")
    add(f"モデル {m['hypCount']} 件 ↔ 確定版 {m['truthCount']} 件"
        f"（対応 {m['paired']} / モデル側だけ {m['extra']} / 確定版側だけ {m['missing']}）")
    add("")

    s = m["start"]
    add("■ 開始のずれ（モデル − 確定版。正なら後ろへずれている）")
    add(f"  中央 {s['median']:+.2f}s   平均 {s['mean']:+.2f}s"
        f"   1 割目 {s['p10']:+.2f}s   9 割目 {s['p90']:+.2f}s"
        f"   最小 {s['min']:+.2f}s   最大 {s['max']:+.2f}s")
    add(f"  {SHIFT_SEC:.0f} 秒以上ずれた行: {m['shiftedCount']} 件"
        f"（後ろ {m['shiftedLate']} / 前 {m['shiftedEarly']}）")
    add("")

    add(f"■ ずれの塊 {m['clusterCount']} か所"
        f"（チャンクあたり {m['clustersPerChunk']} / 10 分あたり {m['clustersPer10min']}）")
    add(f"  直前が長い無音 {m['afterSilence']} か所"
        f" / 直前が重なり {m['afterOverlap']} か所"
        f" / チャンク境界の 15 秒内 {m['nearBoundary']} か所")
    for c in m["clusters"]:
        who = " ".join(f"{k}×{v}" for k, v in c["speakers"].items())
        tags = []
        if c["afterSilence"]:
            tags.append("無音のあと")
        if c["afterOverlap"]:
            tags.append("重なりのあと")
        if c["gapPrev"] is not None:
            tags.append(f"直前との間 {c['gapPrev']:+.2f}s（{c['prevSpeaker']}）")
        add(f"  {_mmss(c['at'])}〜{_mmss(c['until'])}  {c['median']:+.2f}s"
            f"  {c['count']} 行  chunk{c['chunk']}（境界まで {c['toBoundary']}s）"
            f"  {who}{'  ' + '・'.join(tags) if tags else ''}")
        for x in c["samples"][:3]:
            add(f"      {_mmss(x['at'])} {x['d']:+.2f}s {x['speaker']}: "
                f"{x['truth']} ／ モデル: {x['hyp']}")
    add("")

    add("■ 話者別の開始のずれ")
    for k, v in m["bySpeaker"].items():
        add(f"  {k:8s} {v['count']:4d} 件  中央 {v['median']:+.2f}s"
            f"  {SHIFT_SEC:.0f}s 以上 {v['shifted']} 件")
    add("")

    d = m["dropped"]
    add(f"■ 捨てた区間 {d['total']} 件")
    for k, v in sorted(d["byReason"].items(), key=lambda x: -x[1]):
        add(f"  {k}: {v} 件")
    for x in d["samples"][:5]:
        add(f"      {x['reason']} / {x.get('speaker')}: {str(x.get('text'))[:30]}")

    g = m["merged"]
    add(f"■ 重複として 1 本にまとめた組 {g['total']} 件")
    for x in g["samples"][:5]:
        add(f"      {x['text'][:20]}  {x['keptStart']:.2f}s ← {x['dropStart']:.2f}s"
            f"（間 {x['gap']:.2f}s）")

    if m["missingSamples"]:
        add("■ 確定版にあってモデルに無い行（先頭のみ）")
        for x in m["missingSamples"][:6]:
            add(f"      {_mmss(x['at'])} {x['speaker']}: {x['text']}")

    return "\n".join(L)


def _mmss(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 60:d}:{t % 60:02d}"


def load_srt(path: Path) -> list[dict]:
    """.srt を比べる側として読む。ローカル文字起こしの出力を測るためのもの。

    話者は入っていないので「不明」にする。時刻の精度だけを見る。
    """
    from .editor import srt as srt_mod
    cues, _report = srt_mod.parse(path.read_text(encoding="utf-8", errors="replace"))
    out = []
    for c in cues:
        text = str(c.text or "").strip()
        if not text:
            continue
        out.append({"start": float(c.start), "end": float(c.end),
                    "speaker": str(getattr(c, "speaker", "") or "不明"), "text": text})
    out.sort(key=lambda c: (c["start"], c["end"]))
    return out


def load_hyp(path: Path) -> tuple[list[dict], dict]:
    """比べる側を読む。.srt / edit.json / gemini-raw.json のどれでも受ける。"""
    if path.suffix.lower() == ".srt":
        return load_srt(path), {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    if "segments" in doc:                    # gemini-raw.json
        return list(doc["segments"]), doc
    return load_truth(path), {}              # edit.json


def load_truth(path: Path) -> list[dict]:
    """確定版（edit.json）から正解の並びを作る。"""
    doc = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for c in doc.get("cues") or []:
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        out.append({"start": float(c.get("start") or 0.0),
                    "end": float(c.get("end") or 0.0),
                    "speaker": str(c.get("speaker") or "不明"),
                    "text": text})
    out.sort(key=lambda c: (c["start"], c["end"]))
    return out
