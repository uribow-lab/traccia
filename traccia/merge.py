"""複数の文字起こしを 1 本の字幕に合わせる。

Gemini は本文と話者が良く、ローカル（faster-whisper）は時刻が良い。
どちらか一方では足りないので、**本文は Gemini から、時刻はローカルから**取る。

    Gemini 60 秒    ±2 秒に収まる行 79%   対応 384 行
    ローカル small   ±2 秒に収まる行 95%   対応 391 行
    ローカル medium  ±2 秒に収まる行 95%   対応 453 行
    （29 分の素材・人が直し終えた確定版 734 行を正解として実測）

拾える行も違う。Gemini が落とした 223 行をローカルが拾っていて、
そのうち small だけが 44 行、medium だけが 71 行。得意な場所が違うので、
両方を混ぜると確定版の 83% まで届く（Gemini だけなら 52%）。

対応付けの作り方
----------------
diag.align()（順序を保った DP）を使う。**時刻を対応付けの根拠にしない**のが要点で、
Gemini の時刻が 40 秒ずれていても本文だけで正しく結べることは実証済み（TRAC-25）。

合成の規則
----------
    本文 … Gemini を優先。Gemini が拾っていなければローカルの本文
    時刻 … small を優先、無ければ medium、どちらも無ければ Gemini
    話者 … Gemini から。ローカルだけの行は「不明」
    印   … ローカルだけが拾った行、時刻が大きく食い違う行に付ける

印を付けるのは、機械が決めきれなかったところを人がすぐ見つけられるようにするため。
黙って良さそうな方を選んで済ませない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import diag

UNKNOWN = "不明"

# 時刻の出どころを選ぶ優先順。小さいモデルほど時刻が細かい（実測 ±0.5 秒内 58% / 41%）
TIME_PRIORITY = ("small", "medium")

# これ以上食い違っていたら「要確認」の印を付ける
DISAGREE_SEC = 2.0

# 土台の並びを崩す時刻は採らない。ローカルの対応付けが 1 行ずれると、その行が
# 前の行より前へ飛んで順序が入れ替わる（福よし後半の実測で 289 行中 5 件。
# 「じゃあ次回これを」の前に「頼むかって言ったら」が並ぶ、など）。
# 土台（Gemini）の並びは信用してよいので、崩す時刻は使わずに次の候補へ回す。
ORDER_SLACK = 0.0


@dataclass
class Source:
    """合成に混ぜる 1 系統。"""
    name: str                       # "gemini" / "small" / "medium"
    kind: str                       # "text"（本文が良い）/ "time"（時刻が良い）
    segments: list[dict] = field(default_factory=list)


@dataclass
class Report:
    total: int = 0
    fromGemini: int = 0             # 本文を Gemini から取った行
    fromLocal: int = 0              # 本文をローカルから取った行
    timeFrom: dict = field(default_factory=dict)   # 時刻の出どころ別の件数
    disagree: int = 0               # 時刻が大きく食い違った行
    localOnly: int = 0              # ローカルだけが拾った行
    trimmed: int = 0                # 同じ話者の重なりを詰めた（ずらした）行
    reordered: int = 0              # 並びが崩れるので時刻の差し替えを見送った行
    dropped: int = 0                # 二重に拾っていたので落とした行
    joined: int = 0                 # 短い言葉どうしを 1 行にまとめた
    toUnknown: int = 0              # 居場所が無いので「不明」へ移した行
    tooLong: int = 0                # 本文に対して長すぎるので回収しなかった
    shortened: int = 0              # 本文に対して長すぎたので尺を切った

    def to_dict(self) -> dict:
        return {
            "total": self.total, "fromGemini": self.fromGemini,
            "fromLocal": self.fromLocal, "timeFrom": self.timeFrom,
            "disagree": self.disagree, "localOnly": self.localOnly,
            "trimmed": self.trimmed, "dropped": self.dropped,
            "joined": self.joined, "toUnknown": self.toUnknown,
            "tooLong": self.tooLong, "shortened": self.shortened,
        }


def merge(sources: list[Source], *,
          pick_up: bool = False) -> tuple[list[dict], Report]:
    """本文の系統を土台に、時刻の系統を重ねる。

    戻り値の区間は project.import_segments にそのまま渡せる形
    （start / end / speaker / text、必要なら mark）。

    pick_up は「土台が拾えなかった発話を、時刻の系統の本文で足す」かどうか
    （_pick_up_missed）。足すと Gemini が本当に落とした発話を拾えるが、言い直しや
    声が重なった所の聞き間違いも一緒に入る。福よし 21 分の実測では 99 行足して、
    人の確定版に残ったのは 10 行だけだった。既定では足さない。
    """
    text_src = next((s for s in sources if s.kind == "text" and s.segments), None)
    time_srcs = [s for s in sources if s.kind == "time" and s.segments]

    if not text_src and not time_srcs:
        return [], Report()

    # 本文の系統が無ければ、時刻の系統のうち拾いが多いものを土台にする
    if not text_src:
        base = max(time_srcs, key=lambda s: len(s.segments))
        others = [s for s in time_srcs if s is not base]
    else:
        base = text_src
        others = time_srcs

    # 土台の各行に、ほかの系統のどの行が対応するかを集める
    pairs: dict[str, dict[int, dict]] = {}
    for src in others:
        got: dict[int, dict] = {}
        for bi, si in diag.align(base.segments, src.segments):
            if bi is None or si is None:
                continue
            b, s = base.segments[bi], src.segments[si]
            if diag.ratio(b["text"], s["text"]) < diag.MATCH_MIN:
                continue
            got[bi] = s
        pairs[src.name] = got

    rep = Report()
    out: list[dict] = []
    floor: float | None = None             # ここまでに置いた行の開始
    for i, b in enumerate(base.segments):
        start, end = float(b["start"]), float(b["end"])
        origin = base.name
        times: list[tuple[str, float]] = []

        for name in TIME_PRIORITY:
            hit = pairs.get(name, {}).get(i)
            if hit:
                times.append((name, float(hit["start"])))
        # 優先順に無いローカル系統も、食い違いの判定には使う
        for src in others:
            if src.name in TIME_PRIORITY:
                continue
            hit = pairs.get(src.name, {}).get(i)
            if hit:
                times.append((src.name, float(hit["start"])))

        # 時刻を差し替えるのは、土台の並びを崩さないときだけ。前の行より前へ
        # 飛ぶ候補は飛ばして次を見る。どれも崩すなら土台の時刻のまま残す。
        for name, at in times:
            if floor is not None and at < floor - ORDER_SLACK:
                rep.reordered += 1
                continue
            hit = pairs[name][i]
            length = end - start                  # 尺は土台の側を保つ
            start = float(hit["start"])
            end = start + length if length > 0 else float(hit["end"])
            origin = name
            break

        # 目印は付けない。目印は利用者が明示的に付けるもので、機械が付けるもの
        # ではない。実測では 470 件中 151 件（32%）に付いてしまい、目印の意味
        # そのものが薄れた。何が起きたかは取り込み後の要約で伝える。
        if len(times) >= 2 and abs(times[0][1] - times[1][1]) >= DISAGREE_SEC:
            rep.disagree += 1

        text = str(b.get("text") or "").strip()
        speaker = str(b.get("speaker") or "").strip() or UNKNOWN
        if base.kind == "text":
            rep.fromGemini += 1
        else:
            rep.fromLocal += 1
            rep.localOnly += 1

        cue = {"start": round(start, 3), "end": round(max(end, start + 0.2), 3),
               "speaker": speaker, "text": text}
        rep.timeFrom[origin] = rep.timeFrom.get(origin, 0) + 1
        floor = cue["start"]
        out.append(cue)

    # 本文の系統が拾えず、時刻の系統だけが拾った発話を足す
    if text_src and pick_up:
        out.extend(_pick_up_missed(base, others, out, rep))

    out.sort(key=lambda c: (c["start"], c["end"]))
    out = trim_overlong(out, rep)
    out = resolve_same_speaker_overlaps(out, rep)
    rep.total = len(out)
    return out, rep


# 本文がこれ以上似ていて時間も重なっていたら、同じ発話を二重に拾ったとみなす
DUP_SIM = 0.7

# 詰めてもずらしても居場所が無いとき、この長さ以内どうしなら 1 行にまとめる。
# 相槌や短い返事なら、まとめても字幕として自然に読める。
SHORT_CHARS = 8


def resolve_same_speaker_overlaps(cues: list[dict], rep: "Report") -> list[dict]:
    """同じ話者どうしの重なりを無くす。

    同じ人が同時に二言喋ることはないので、この重なりは**必ず直すことになる**。
    エディタは注意マークで教えてくれるが、最後まで直すと決まっているものを人が
    1 つずつ直すのは無駄なので、出す時点で済ませる。

    実測（470 件）では 83 件（18%）が重なっていた。中身は 2 通りある。

        B 60.34〜62.14 食べた瞬間に
          60.34〜62.54 食べた瞬間に、なんか     ← ほぼ同じ本文。二重に拾っている
        A 75.34〜78.54 ちょっとあるね
          75.70〜76.50 ピザとか                ← 別の発話。詰めれば済む

    前者は長いほうへまとめる。後者は前の行の終わりを詰め、詰めると短くなりすぎる
    場合は次の行の開始を後ろへずらす。

    それでも居場所が無い（片方がもう片方に丸ごと入っている）ときは、

        短い言葉どうし  … 1 行にまとめる（「黒糖のレベル？ へえ」）
        長い発話が絡む  … あとの行を「不明」へ移す

    落とさないのが要点。中身が消えるより、人が話者を付け直すほうがよい。

    **別の話者との重なりは残す。** 同時発話はふつうにあるので、そこは人の判断。
    """
    # 3 つ以上が連鎖して重なっていると、1 回の走査では取り切れない。前の行を
    # 詰めた結果が、さらに次の行と衝突するため。取り切るまで繰り返す。
    # 実測では 1 回で 83 → 2 件、2 回目で 0 件になった。
    for _ in range(4):
        cues, left = _resolve_once(cues, rep)
        if not left:
            break
    return cues


def _resolve_once(cues: list[dict], rep: "Report") -> tuple[list[dict], int]:
    """1 周ぶん。戻り値は (結果, まだ残っている重なりの数)。"""
    by: dict[str, list[dict]] = {}
    for c in cues:
        by.setdefault(c.get("speaker") or UNKNOWN, []).append(c)

    drop: set[int] = set()
    for lst in by.values():
        lst.sort(key=lambda c: (c["start"], c["end"]))
        for i in range(len(lst) - 1):
            a, b = lst[i], lst[i + 1]
            if id(a) in drop:
                continue
            if a["end"] <= b["start"] + 0.001:
                continue

            # 同じ発話を二重に拾っている場合は、長いほうを残して片方を落とす
            if diag.ratio(a["text"], b["text"]) >= DUP_SIM:
                loser = a if len(a["text"]) < len(b["text"]) else b
                keeper = b if loser is a else a
                keeper["start"] = round(min(a["start"], b["start"]), 3)
                keeper["end"] = round(max(a["end"], b["end"]), 3)
                drop.add(id(loser))
                rep.dropped += 1
                continue

            # 前の行の終わりを詰める
            new_end = round(b["start"] - GAP, 3)
            if new_end - a["start"] >= MIN_DUR:
                a["end"] = new_end
                rep.trimmed += 1
                continue

            # 詰めきれない。次の行の開始を後ろへずらす
            new_start = round(a["end"] + GAP, 3)
            if b["end"] - new_start >= MIN_DUR:
                b["start"] = new_start
                rep.trimmed += 1
                continue

            # どちらも潰れる。片方がもう片方に丸ごと入っているような場合で、
            # 時間をいじって両方を残すことはできない。
            #
            # 短い言葉どうしなら 1 行にまとめる。相槌や短い返事は、まとめても
            # 字幕として自然に読める（「黒糖のレベル？ へえ」）。
            if len(a["text"]) <= SHORT_CHARS and len(b["text"]) <= SHORT_CHARS:
                a["text"] = f"{a['text']} {b['text']}"
                a["end"] = round(max(a["end"], b["end"]), 3)
                drop.add(id(b))
                rep.joined += 1
                continue

            # 長い発話が絡むならまとめられない。話者の割り当てが違っている
            # 可能性が高いので、**落とさずに**あとの行を「不明」へ移す。
            # 中身が消えるより、人が話者を付け直すほうがよい。
            b["speaker"] = UNKNOWN
            rep.toUnknown += 1

    out = [c for c in cues if id(c) not in drop]

    left = 0
    for spk in {c.get("speaker") or UNKNOWN for c in out}:
        lst = sorted((c for c in out if (c.get("speaker") or UNKNOWN) == spk),
                     key=lambda c: (c["start"], c["end"]))
        left += sum(1 for i in range(len(lst) - 1)
                    if lst[i]["end"] - lst[i + 1]["start"] > 0.001)
    return out, left


# 回収した区間が長すぎないかを見るための値。人が作った字幕 4,717 行から測った。
#
#   1 文字あたりの尺  中央 0.154 秒 / 9 割目 0.288 秒
#    1〜 2 文字        中央 0.64s / 9 割目 1.00s
#    6〜10 文字        中央 1.29s / 9 割目 2.00s
#   11〜20 文字        中央 1.83s / 9 割目 2.70s
#
# whisper は無音を巻き込んで区間を伸ばすことがある（「で」1 文字に 13.39 秒、
# 「おめでとうございます」に 17.84 秒）。本文の長さから妥当な尺を見積もって切る。
SEC_PER_CHAR = 0.30        # 9 割目より少し緩く取る
BASE_SEC = 0.6             # 1 文字でもこれだけは要る
MAX_PICK_SEC = 4.0         # これを超える回収は、切ってもなお疑わしい

# 回収するとき、時間が重なる行と本文がこれ以上似ていたら同じ発話とみなす
PICK_DUP_SIM = 0.5

# 同じ話者の行を詰めたあと、これより短くなるなら詰めきれないとみなす
MIN_DUR = 0.25

# 詰めるときに空ける隙間。0 だと境目が接して、また重なりとして扱われる
GAP = 0.02


def _plausible_end(seg: dict, text: str) -> float:
    """本文の長さから見て妥当な終わり。長すぎる区間を切る。

    頭を残して切るのは、whisper が伸ばすのはたいてい後ろ側だから
    （発話のあとの無音を区間に含めてしまう）。
    """
    limit = BASE_SEC + len(text) * SEC_PER_CHAR
    return min(seg["end"], seg["start"] + limit)


def trim_overlong(cues: list[dict], rep: "Report") -> list[dict]:
    """本文に対して長すぎる区間を切る。

    回収した行だけでなく、土台になった系統にも同じことが起きる。ローカルだけで
    字幕を作るとき、土台は whisper の出力そのものなので、無音を巻き込んだ長い
    区間がそのまま残っていた（実測で 168 件中 14 件が 4 秒以上）。

    切るのは終わり側だけで、始まりは動かさない。whisper が伸ばすのは後ろなので、
    頭の位置は当たっていることが多い。
    """
    for c in cues:
        text = str(c.get("text") or "")
        end = _plausible_end(c, text)
        if end < c["end"] - 0.05:
            c["end"] = round(max(end, c["start"] + MIN_DUR), 3)
            rep.shortened += 1
    return cues


def _overlap(a: dict, b: dict) -> float:
    """2 区間が重なっている秒数。"""
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def _pick_up_missed(base: Source, others: list[Source], merged: list[dict],
                    rep: Report) -> list[dict]:
    """土台が拾えなかった発話を、時刻の系統から回収する。

    実測では Gemini が落とした 223 行をローカルが拾っていた。本文の精度は落ちるが、
    「そこで誰かが喋った」ことが分かるだけでも直す手間が変わる。印を付けて残す。

    ここで気をつけるのは**同じ発話を二重に置かないこと**。本文で対応が取れなくても、
    区切り方が違うだけで同じ音を指していることが多い（Gemini が 1 行にまとめた
    ところを small が 3 行に割る、など）。本文の似かたでは弾けないので、
    **時間の重なり**で判断する。自分の尺の半分以上が既にある行と重なっていれば、
    それは新しい発話ではない。

    この判定を入れないと、29 分の素材で 1341 件（確定版は 734 件）まで膨らんだ。
    """
    picked: list[dict] = []
    have = sorted(merged, key=lambda c: c["start"])

    def covered(seg: dict, text: str) -> bool:
        """すでに入っている行と同じ発話か。

        時間の重なりだけで見ていたころは、**ローカルが別の区切りで拾った同じ
        発話**がすり抜けた。重なりが自分の尺の半分に届かないためで、実測では
        19 件が二重に入っていた。

            C   48.02〜49.52 え、これは？
            不明 49.02〜52.34 これは        ← 重なりは自分の尺の 15%

        少しでも時間が重なっている行とは、本文も見比べる。片方がもう片方に
        含まれる場合（「いいじゃん」⊂「劇的劇的変化でいいじゃん」）も同じ発話。
        """
        span = seg["end"] - seg["start"]
        if span <= 0:
            return True
        na = diag.normalize(text)
        for c in have:
            if c["start"] > seg["end"]:
                break
            if _overlap(seg, c) <= 0:
                continue
            if _overlap(seg, c) >= span * 0.5:
                return True
            nb = diag.normalize(c["text"])
            if na and nb and (na in nb or nb in na):
                return True
            if diag.ratio(text, c["text"]) >= PICK_DUP_SIM:
                return True
        return False

    for src in others:                     # TIME_PRIORITY の順に見る
        for s in src.segments:
            text = str(s.get("text") or "").strip()
            if not text:
                continue
            seg = {"start": float(s["start"]), "end": float(s["end"])}
            # 本文に対して長すぎる区間は切る。切ってもなお長ければ回収しない
            seg["end"] = _plausible_end(seg, text)
            if seg["end"] - seg["start"] > MAX_PICK_SEC:
                rep.tooLong += 1
                continue
            if covered(seg, text):
                continue
            cue = {"start": round(seg["start"], 3), "end": round(seg["end"], 3),
                   "speaker": UNKNOWN, "text": text}
            picked.append(cue)
            have = sorted(have + [cue], key=lambda c: c["start"])
            rep.localOnly += 1
            rep.fromLocal += 1
            rep.timeFrom[src.name] = rep.timeFrom.get(src.name, 0) + 1
    return picked


def summary_text(rep: Report) -> str:
    """画面に 1 行で出す説明。"""
    parts = [f"{rep.total} 件"]
    if rep.fromGemini:
        parts.append(f"本文は Gemini から {rep.fromGemini} 件")
    if rep.localOnly:
        parts.append(f"ローカルだけが拾ったもの {rep.localOnly} 件")
    if rep.timeFrom:
        who = "・".join(f"{k} {v}" for k, v in sorted(rep.timeFrom.items()))
        parts.append(f"時刻の出どころ {who}")
    if rep.shortened:
        parts.append(f"長すぎた尺を切ったもの {rep.shortened} 件")
    if rep.reordered:
        parts.append(f"並びを崩すので時刻を採らなかったもの {rep.reordered} 件")
    fixed = rep.trimmed + rep.dropped + rep.joined + rep.toUnknown
    if fixed:
        detail = []
        if rep.trimmed:
            detail.append(f"詰め {rep.trimmed}")
        if rep.dropped:
            detail.append(f"二重をまとめ {rep.dropped}")
        if rep.joined:
            detail.append(f"1 行にまとめ {rep.joined}")
        if rep.toUnknown:
            detail.append(f"不明へ {rep.toUnknown}")
        parts.append(f"同じ話者の重なりを直したもの {fixed} 件"
                     f"（{'・'.join(detail)}）")
    return " / ".join(parts)
