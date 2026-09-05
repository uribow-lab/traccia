"""Gemini に音声を渡して、話者付きの字幕を作る。

bench/gemini.py の実験を、1 本まるごとの動画で回せる形にしたもの。
違いは 3 つ。

  ・動画から音声を取り出して一定長に切る。1 時間ぶんを 1 回では送れない
  ・チャンクをまたいで話者が入れ替わらないよう、直前の発話を一緒に渡す
  ・実測トークンを 1 回ずつ記録して、いくら使ったかが後から見えるようにする

専用 ASR（Deepgram など）と違って、Gemini には「この語が出てくる」「何人の
会話だ」と教えられる。店名や地名のような耳慣れない語はそこで効く。
そのぶん高い（既定の 3.7 Flash の実測で 1 時間あたり $0.50 前後）ので、
使う / 使わないの切り替えは traccia/config.py 側で持っている。
"""

from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

API = "https://generativelanguage.googleapis.com/v1beta/models"

RATE = 16000              # Gemini に渡す音声のサンプリングレート

# 診断ファイル（<名前>.gemini-raw.json）の形式の版。
DIAG_VERSION = 1

# モデルが送った音声より先の時刻を返したとき、これを超えたら「時刻が壊れている」
# とみなす。音声の長さを超える時刻は物理的にありえないので、無料で使える手がかり。
# 240 秒では 8 本中 4 本、60 秒でも 29 本中 8 本が該当した（TRAC-25）。
OVERRUN_LIMIT = 2.0

# チャンクの前後にこれだけ余分に音を付ける。文の途中で切れた発話を
# モデルが最後まで聞けるようにするためで、区間の採用はあくまで
# 「中心が担当範囲に入るか」で決める（_merge を参照）。
OVERLAP_SEC = 3.0

# 話者の対応付けのために、直前のチャンクの末尾から渡す発話の数。
CARRY_SEGMENTS = 4

TIMEOUT_SEC = 600
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
RETRY_WAIT = (5, 15, 40)  # 何秒あけて何回まで試すか

# 100 万トークンあたりの単価（USD）。単価は変わるので現行の料金表で確認すること。
#   https://ai.google.dev/gemini-api/docs/pricing
#
# 3.6 / 3.7 / 3.8 Flash の値は 2026-12-31 までの割引価格。2027-01-01 に
# in 1.50 / out 7.50 へ戻る。年が明けたらここを直す。
#
# 音声とテキストで単価が分かれるモデルは音声のほうを書く。traccia が送るのは音声。
#
# 注意: 思考トークン（thoughtsTokenCount）も出力として課金される。
# 実測では出力の 7 割強を思考が占めた。本文の長さだけで見積もると大きく外す。
PRICING = {
    "gemini-3.6-flash":       {"in": 0.75, "out": 3.75},
    "gemini-3.7-flash":       {"in": 0.75, "out": 3.75},
    "gemini-3.8-flash":       {"in": 0.75, "out": 3.75},
    "gemini-3.5-flash-lite":  {"in": 0.30, "out": 2.50},
    "gemini-3-flash-preview": {"in": 1.00, "out": 3.00},
}
PRICING_FALLBACK = {"in": 1.50, "out": 7.50}

# 画面のモデル選択に出す候補。ここに無いモデルでも設定に直接書けば通る。
# 名前は入れ替わりが早い。載せる前に bench/gemini_models.py で実在を確かめること
# （`gemini-3-flash` は一覧から消えて 404 になっていた）。
#
# `gemini-3.5-transcribe` は**載せられない**。JSON モードに対応しておらず
# （HTTP 400 "JSON mode is not enabled for this model"）、素の書き起こしが
# 1 本返るだけで時刻も話者も付かない。使うなら話者分離と時刻合わせを別に
# 用意することになり、Flash 系の置き換えにはならない。
#
# 3.6 / 3.7 / 3.8 は福よし 10 分で実測した（docs/api-keys.md「モデルを選ぶ」）。
MODEL_CHOICES = [
    {"id": "gemini-3.7-flash",       "label": "3.7 Flash（既定・話者と時刻が最も安定）"},
    {"id": "gemini-3.8-flash",       "label": "3.8 Flash（時刻は僅かに上。話者は 3.7 に劣る）"},
    {"id": "gemini-3.6-flash",       "label": "3.6 Flash（旧既定・話者分離が働かない）"},
    {"id": "gemini-3.5-flash-lite",  "label": "3.5 Flash Lite（安い・取りこぼしは増える）"},
    {"id": "gemini-3-flash-preview", "label": "3 Flash（preview）"},
]

# 見積もりに使う 1 秒あたりのトークン数。
#   入力 … 音声は 1 秒 ≒ 32 トークン（Gemini の数え方）
#   出力 … 福よし 10 分の実測から逆算。思考トークンの量がモデルで大きく
#           違うので、1 つの値では倍近く外れる（3.6 は 3.7 の 1.9 倍出す）。
#           発話の密度でも振れるので、あくまで目安。
AUDIO_TOKENS_PER_SEC = 32
OUTPUT_TOKENS_PER_SEC = {
    "gemini-3.7-flash": 30,      # 実測 18,013 tok / 600 秒（思考 22%）
    "gemini-3.8-flash": 32,      # 実測 19,177 tok / 600 秒（思考 24%）
    "gemini-3.6-flash": 57,      # 実測 34,442 tok / 600 秒（思考 62%）
}
OUTPUT_TOKENS_PER_SEC_FALLBACK = 68   # 測っていないモデルは高いほうに倒す


class GeminiError(Exception):
    """呼び出し側に見せるエラー。文面はそのまま画面に出る。"""


class Cancelled(Exception):
    """利用者が途中で止めた。"""


# ---------------------------------------------------------------- 見積もり

def price_of(model: str) -> dict:
    return PRICING.get(model, PRICING_FALLBACK)


def estimate(duration: float, model: str, chunk_sec: int) -> dict:
    """流す前の想定額。実測ではないので「だいたい」以上のことは言えない。"""
    price = price_of(model)
    chunks = max(1, int(-(-duration // max(1, chunk_sec))))  # 切り上げ
    # 前後の重なりぶんは余分に音を送るので、その秒数も入力に乗る
    audio_sec = duration + max(0, chunks - 1) * OVERLAP_SEC * 2
    in_tok = audio_sec * AUDIO_TOKENS_PER_SEC
    out_tok = duration * OUTPUT_TOKENS_PER_SEC.get(
        model, OUTPUT_TOKENS_PER_SEC_FALLBACK)
    cost = (in_tok * price["in"] + out_tok * price["out"]) / 1e6
    return {
        "model": model,
        "duration": round(duration, 1),
        "chunks": chunks,
        "chunkSec": chunk_sec,
        "inputTokens": int(in_tok),
        "outputTokens": int(out_tok),
        "pricePerMTok": price,
        "cost": round(cost, 4),
        "costPerHour": round(cost * 3600 / duration, 3) if duration > 0 else 0.0,
    }


def read_usage(data: dict, model: str) -> dict:
    """応答に入っている実測トークンから、この 1 回の額を出す。"""
    u = data.get("usageMetadata") or {}
    prompt = int(u.get("promptTokenCount") or 0)
    answer = int(u.get("candidatesTokenCount") or 0)
    thoughts = int(u.get("thoughtsTokenCount") or 0)
    price = price_of(model)
    cost = (prompt * price["in"] + (answer + thoughts) * price["out"]) / 1e6
    return {
        "promptTokens": prompt,
        "answerTokens": answer,
        "thoughtsTokens": thoughts,
        "outputTokens": answer + thoughts,
        "cost": cost,
    }


# ---------------------------------------------------------------- HTTP

def _request(url: str, body: dict | None = None, timeout: int = TIMEOUT_SEC,
             cancel=None) -> dict:
    """1 回ぶんの呼び出し。混雑・一時障害は間をあけて数回まで試す。

    長い動画では 10 回以上続けて投げるので、途中の 1 回が 503 で落ちただけで
    全部やり直しになるのは割に合わない。
    """
    last = ""
    for attempt in range(len(RETRY_WAIT) + 1):
        if cancel and cancel():
            raise Cancelled()
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method="POST" if data is not None else "GET",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            last = f"HTTP {e.code}: {detail}"
            if e.code == 400 and "API key" in detail:
                raise GeminiError("API キーが正しくありません") from e
            if e.code in (401, 403):
                raise GeminiError(f"API キーが拒否されました（{e.code}）。"
                                  f"キーと課金の設定を確認してください") from e
            if e.code == 404:
                raise GeminiError(f"モデルが見つかりません（404）。"
                                  f"モデル名を確認してください: {detail[:150]}") from e
            if e.code not in RETRY_STATUS:
                raise GeminiError(last) from e
        except urllib.error.URLError as e:
            last = f"接続できません: {e.reason}"
        except TimeoutError:
            last = f"応答がありません（{timeout} 秒）"

        if attempt >= len(RETRY_WAIT):
            break
        wait = RETRY_WAIT[attempt]
        # 待っている間もキャンセルに反応する
        for _ in range(wait * 2):
            if cancel and cancel():
                raise Cancelled()
            time.sleep(0.5)

    raise GeminiError(last or "呼び出しに失敗しました")


def check_key(key: str) -> dict:
    """キーが通るかを確かめる。生成には使わないので費用はかからない。"""
    if not key:
        raise GeminiError("API キーが設定されていません")
    url = f"{API}?key={urllib.parse.quote(key)}&pageSize=200"
    data = _request(url)
    models = []
    for m in data.get("models") or []:
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        name = str(m.get("name") or "")
        models.append(name.split("/")[-1])
    return {"ok": True, "models": sorted(models)}


# ---------------------------------------------------------------- 音声

def load_audio(video: Path):
    """動画から 16kHz mono の int16 を作る。

    1 時間で 115MB ほどメモリに乗る。話者分離（float32）の半分なので、
    それが通る環境ならここも通る。
    """
    try:
        import av
        import numpy as np
    except ImportError as e:
        raise GeminiError("PyAV と numpy が必要です（requirements.txt を入れてください）") from e

    try:
        container = av.open(str(video))
    except Exception as e:  # noqa: BLE001
        raise GeminiError(f"動画を開けません: {e}") from e

    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise GeminiError("この動画には音声トラックがありません")
        resampler = av.audio.resampler.AudioResampler(
            format="s16", layout="mono", rate=RATE)
        parts = []
        for frame in container.decode(stream):
            for rf in resampler.resample(frame):
                parts.append(rf.to_ndarray().reshape(-1))
        for rf in resampler.resample(None) or []:
            parts.append(rf.to_ndarray().reshape(-1))
    finally:
        container.close()

    if not parts:
        raise GeminiError("音声を読み取れませんでした")
    return np.concatenate(parts).astype("int16")


def _wav_bytes(samples) -> bytes:
    """int16 mono を WAV にする。一時ファイルは作らない。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


def write_peaks(samples, peaks_out: Path) -> bool:
    """すでに読んだ音声から波形も作っておく。動画を読み直さずに済む。"""
    try:
        from . import peaks as peaks_mod
        flt = samples.astype("float32") / 32768.0
        peaks_mod.save(peaks_out, peaks_mod.build(flt, RATE))
        del flt
        return True
    except Exception:  # noqa: BLE001
        # 波形が無くても字幕は使えるので、ここで止めない
        return False


# ---------------------------------------------------------------- プロンプト

# 区切りの指示の既定値。style.py が測れないときにこれが入る。
DEFAULT_STYLE_LINES = (
    "- 区間は短く刻む。**1 区間 1.5 秒前後・10 文字前後**が目安。"
    "長くても 3 秒・20 文字まで\n"
    "- 「うん」「まあ」のようなつなぎは、意味が変わらなければ省いてよい\n"
)

# 区間の長さの目安。ここを実測に合わせると、人の区切りと噛み合う数が大きく変わる。
#
#   これまで（1〜5 秒）  570 行 / 1 行 2.20 秒 / 拾えた行 67%
#   実測に合わせた       913 行 / 1 行 1.50 秒 / 拾えた行 75%
#   人が作った確定版      734 行 / 1 行 1.43 秒
#
# 総字数は 7,953 → 8,041 字でほぼ変わらない（確定版は 7,833 字）。
# 内容が増えたのではなく、同じ内容を細かく割っただけ。時刻の精度は動かない
# （±2 秒内 79% → 80%。そちらはローカル文字起こしとの併用で解いている）。
#
# ただしこの 1.5 秒は「この現場の作法」であって普遍の正解ではない。手元の
# 確定版 5 本でも 1.26〜1.94 秒の幅がある。本来はセットごとに実測から決めたい
# （TRAC-24）。ここに置いているのは、まだ確定版が無いときの出発点。
PROMPT = """この音声を書き起こして、字幕として使える形で返してください。

条件:
- 日本語。分かち書きしない（語の間に空白を入れない）
- 1 区間は 1 発話。相槌や短い返事も 1 区間として拾う
{style}- 1 文が長いときは、息継ぎ・読点・助詞の切れ目で分ける。
  「AだからB」なら「Aだから」と「B」の 2 区間にする
- 話者が変わったら必ず区間を分ける
- 聞き取れない箇所は無理に埋めず、その区間を省く
- 時刻はこの音声の先頭を 0.0 秒とした秒数（小数第 2 位まで）
- speaker には「A」「B」のように、名前だけを入れる（「話者A」とは書かない）
"""


def build_prompt(*, speakers: list[str] | None, num_speakers: int | None,
                 terms: list[str] | None, note: str | None,
                 carry: list[dict] | None, part: tuple[int, int] | None,
                 style: str | None = None) -> str:
    # 区切りの指示は、そのセットの確定版から測った値を差し込む（traccia/style.py）。
    # 測れないときは既定値。素材ごとに 1 行の尺は 1.26〜1.94 秒の幅があるので、
    # 1 つの固定値では吸収できない。
    p = PROMPT.replace("{style}", style or DEFAULT_STYLE_LINES)

    if part and part[1] > 1:
        p += (f"\nこれは長い収録を切り分けた {part[1]} 本のうちの {part[0]} 本目です。"
              "前後は別の音声として送っています。この音声の中だけで時刻を数えてください。\n")

    if speakers:
        p += ("\n登場する話者は次の名前で呼んでください。"
              "同じ人には必ず同じ名前を使います:\n  "
              + " / ".join(speakers) + "\n")
    elif num_speakers:
        p += f"\nこの音声には {num_speakers} 人が登場します。\n"

    if carry:
        lines = "\n".join(f"  {c['speaker']}: {c['text']}" for c in carry)
        p += ("\n直前まで、この会話は次のように続いていました。"
              "同じ人には同じ名前を使ってください（この部分は書き起こしに含めない）:\n"
              + lines + "\n")

    if terms:
        p += ("\n次の固有名詞が出てきます。これらは正しくこの表記で書いてください:\n  "
              + " / ".join(terms) + "\n")

    if note:
        p += f"\n場面の説明: {note}\n"
    return p


SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "speaker": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["start", "end", "speaker", "text"],
            },
        }
    },
    "required": ["segments"],
}


def join_ja(s: str) -> str:
    """日本語の間に入った空白を取る。英単語の間の空白は残す。"""
    s = s.replace("\n", " ")
    return re.sub(r"(?<=[^\x00-\x7F])\s+|\s+(?=[^\x00-\x7F])", "", s).strip()


# 空白を前に置きたくない文字。「そうですね。」を「そうですね 」にしないためのもの。
# 閉じ括弧と句読点の類だけを見る。開き括弧の後ろは「。」がまず来ないので触らない。
CLOSING_RE = re.compile(r"\s+(?=[」』）〉》】〕］｝、！？…‥)\]},.!?])")


def drop_periods(s: str) -> str:
    """本文から「。」を取る。文の途中なら半角スペース、行末なら何も残さない。

    字幕は 1 区間が 1 発話なので、末尾の「。」は場所を取るだけで意味が無い。
    途中の「。」は文の切れ目が見えなくなると読みにくいので、空白に置き換える。
    「。」を空白にして前後を詰めれば、どちらの場合も同じ 1 つの規則で片づく。

    ただし閉じ括弧の直前だけは詰める。「そうですね。」がそのままだと
    「そうですね 」となり、括弧の中に空白が浮いて見えるため。
    """
    t = re.sub(r"\s+", " ", str(s or "").replace("。", " "))
    return CLOSING_RE.sub("", t).strip()


SPEAKER_PREFIX_RE = re.compile(r"^\s*話者\s*")


def clean_speaker(raw: str) -> str:
    """「話者A」「 A 」などを揺れなく「A」に寄せる。"""
    name = SPEAKER_PREFIX_RE.sub("", str(raw or "")).strip()
    name = name.strip("：:").strip()
    return name or "不明"


# ---------------------------------------------------------------- 本体

def _call_chunk(*, key: str, model: str, audio: bytes, prompt: str,
                cancel=None) -> tuple[list[dict], dict]:
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "audio/wav",
                                 "data": base64.b64encode(audio).decode("ascii")}},
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0.0,
        },
    }
    url = f"{API}/{model}:generateContent?key={urllib.parse.quote(key)}"
    data = _request(url, body, cancel=cancel)
    usage = read_usage(data, model)

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        segs = json.loads(text)["segments"]
    except Exception as e:  # noqa: BLE001
        reason = (data.get("candidates") or [{}])[0].get("finishReason")
        if reason == "MAX_TOKENS":
            raise GeminiError(
                "応答が途中で切れました（出力上限）。"
                "設定で 1 回に送る長さを短くしてください") from e
        raise GeminiError(f"応答を解釈できません（finishReason={reason}）: {e}") from e

    return (segs if isinstance(segs, list) else []), usage


def _accept(segs: list[dict], *, offset: float, lo: float, hi: float,
            duration: float, is_last: bool, sent_lo: float = 0.0,
            sent_hi: float = 0.0, dropped: list[dict] | None = None) -> list[dict]:
    """このチャンクが担当する範囲の区間だけを拾う。

    前後に余分な音を付けて送っているので、境界の発話は隣のチャンクにも
    現れる。区間の**中心**が担当範囲に入るものだけ採ることで、
    どちらか一方にだけ残る。またぐ発話は、それを最後まで聞けた側が採る。

    dropped を渡すと、捨てた区間とその理由をそこに積む。診断で
    「行が消えた」ことを黙って見逃さないため（traccia/diag.py）。
    """
    def drop(reason: str, raw: dict, **extra) -> None:
        if dropped is None:
            return
        dropped.append({"reason": reason,
                        "rawStart": raw.get("start"), "rawEnd": raw.get("end"),
                        "speaker": raw.get("speaker"), "text": raw.get("text"),
                        **extra})

    out = []
    for s in segs:
        try:
            start = float(s["start"]) + offset
            end = float(s["end"]) + offset
        except (KeyError, TypeError, ValueError):
            drop("時刻が読めない", s)
            continue
        # join_ja が先。逆にすると、「。」の代わりに入れた半角スペースを
        # join_ja が「日本語の間の空白」とみなして消してしまう。
        text = drop_periods(join_ja(str(s.get("text") or "")))
        if not text:
            drop("本文が空", s)
            continue

        # モデルは音声の外の時刻を返すことがある。先に音声の中へ収める。
        # ここを通さないと開始が終了より後ろの区間ができて、字幕として壊れる。
        start = max(0.0, start)
        if start >= duration:
            drop("音声の外", s, start=start, end=end)
            continue
        end = min(duration, max(end, start))
        if end - start < 0.15:
            end = min(duration, start + 0.4)
        if end <= start:
            drop("尺が無い", s, start=start, end=end)
            continue

        center = (start + end) / 2
        if center < lo or (center >= hi and not is_last):
            # モデルの時刻が正確という前提の振り分けなので、時計が狂うと
            # ここで落ちる。隣のチャンクも自分の範囲で弾くため、両方から
            # 消えて行が失われる（60 秒でも 71 件が消えていた）。
            #
            # ただし、はみ出しが重なりの幅に収まっているなら話が違う。それは
            # 境界をまたいだ発話が少し寄っただけで、隣も同じ理由で弾いてしまう。
            # 端へ寄せて拾う。
            #
            # 大きく外れているもの（40 秒級のドリフト）は寄せない。まとめて
            # 境界に積み上がって、かえって直しにくくなる。はみ出しの警告
            # （OVERRUN_LIMIT）で「この範囲は時刻が壊れている」と伝えるほうがよい。
            near = OVERLAP_SEC + 1.0
            if lo - near <= center < lo or hi <= center <= hi + near:
                # 担当範囲の外でも、その音は実際に送っている（前後に OVERLAP_SEC
                # ぶん余分に付けているため）。聞こえていた音に対する時刻なら
                # 信用してよいので、境界へ押し込まずそのまま採る。
                #
                # 押し込んでいた頃は、境界に行が積み上がって本来の位置から
                # 最大 2 秒ずれていた（TRAC-35）。福よし 10 分で寄せた 13 行を
                # 正解と突き合わせると、誤差の中央は 1.76 秒 → 0.31 秒になり、
                # 13 行すべてで時刻が送った音の範囲に収まっていた。
                #
                # ずれたままにすると _dedupe の 2 秒窓から外れて、隣のチャンクが
                # 出した同じ発話を重複と見抜けなくなる（TRAC-36）。本来の時刻
                # どうしなら重なるので、そちらでも都合がよい。
                if sent_lo <= center <= sent_hi:
                    out.append({"start": start, "end": min(end, duration),
                                "speaker": clean_speaker(s.get("speaker")),
                                "text": text})
                    drop("担当範囲の外だが送った音の中", s,
                         start=start, end=end, center=center)
                    continue

                # 送った音より外の時刻を指している。モデルの時計が狂っている
                # ので、位置は当てにできない。それでも落とすと行が消えるため、
                # 従来どおり端へ寄せて拾う。
                shifted = min(max(start, lo), max(lo, hi - 0.4))
                end = shifted + max(0.4, end - start)
                start = shifted
                out.append({"start": start, "end": min(end, duration),
                            "speaker": clean_speaker(s.get("speaker")),
                            "text": text})
                drop("担当範囲へ寄せた", s, start=start, end=end, center=center)
                continue
            drop("中心が担当範囲の外", s, start=start, end=end, center=center)
            continue
        out.append({"start": start, "end": end,
                    "speaker": clean_speaker(s.get("speaker")), "text": text})
    return out


def _dedupe(segs: list[dict], merged: list[dict] | None = None) -> list[dict]:
    """同じ文言が同じ場所に二重に入っているものを落とす。

    中心での振り分けで大半は防げるが、モデルが同じ発話を少しずれた時刻で
    返すことがあるので、最後にもう一度だけ見る。

    merged を渡すと、1 本にまとめた組をそこに積む。相槌の連発のように
    正当な繰り返しまで潰していないかを診断で見るため。
    """
    segs.sort(key=lambda s: (s["start"], s["end"]))
    out: list[dict] = []
    for s in segs:
        dup = next((p for p in reversed(out[-6:])
                    if p["text"] == s["text"] and abs(p["start"] - s["start"]) < 2.0), None)
        if dup:
            if merged is not None:
                merged.append({"text": s["text"],
                               "keptStart": dup["start"], "keptEnd": dup["end"],
                               "dropStart": s["start"], "dropEnd": s["end"],
                               "gap": round(s["start"] - dup["start"], 3),
                               "speaker": s.get("speaker")})
            dup["end"] = max(dup["end"], s["end"])
            continue
        out.append(s)
    return out


def transcribe(video: Path, *, key: str, model: str, chunk_sec: int,
               terms: list[str] | None = None, note: str = "",
               speakers: list[str] | None = None, num_speakers: int | None = None,
               peaks_out: Path | None = None, diag_out: Path | None = None,
               style: str | None = None, start_sec: float = 0.0,
               dur_sec: float | None = None,
               progress=None, cancel=None) -> dict:
    """動画 1 本を書き起こす。戻り値の segments はそのまま字幕にできる。

    progress(dict) は段階ごとに呼ばれる。cancel() が True を返したら
    Cancelled を送出して途中で降りる（そこまでに使った額は返り値に残す）。

    diag_out を渡すと、モデルが返した生の時刻をチャンクの範囲つきで
    そこへ書く。時刻のずれがどこで生まれているかを追うためのもので、
    字幕そのものには影響しない（traccia/diag.py が読む）。

    start_sec / dur_sec で流す範囲を絞れる。正解が動画の途中までしか
    無いときに、その範囲だけへ課金を絞るためのもの（診断用）。音声は
    切り出さず、送るチャンクの範囲だけを狭める。**返る時刻は元動画の
    ままなので、正解の edit.json とそのまま突き合わせられる。**
    """
    def notify(**kw):
        if progress:
            progress(kw)

    if not key:
        raise GeminiError("API キーが設定されていません")

    notify(phase="audio", message="音声を取り出しています…")
    samples = load_audio(video)
    duration = samples.size / RATE
    if duration < 0.2:
        raise GeminiError("音声が短すぎます")

    # 流す範囲。既定は全体。音声そのものは切らずに、チャンクの割り当てを
    # この範囲に閉じる。こうすると lo / hi も返る時刻も元動画のままになる。
    clip_lo = min(max(0.0, float(start_sec)), duration)
    clip_hi = duration if dur_sec is None else min(duration, clip_lo + float(dur_sec))
    if clip_hi - clip_lo < 0.2:
        raise GeminiError(f"指定された範囲に音声がありません"
                          f"（{_mmss(clip_lo)}〜{_mmss(clip_hi)}）")
    if peaks_out and (clip_lo, clip_hi) != (0.0, duration):
        # 波形は動画全体のものでないと、エディタの表示とずれる。
        raise GeminiError("範囲を絞るときは波形を作り直せません")

    if peaks_out:
        notify(phase="audio", message="波形を作っています…")
        write_peaks(samples, peaks_out)

    if cancel and cancel():
        raise Cancelled()

    chunk_sec = max(30, int(chunk_sec))
    total_chunks = max(1, int(-(-(clip_hi - clip_lo) // chunk_sec)))
    over = int(OVERLAP_SEC * RATE)

    all_segs: list[dict] = []
    totals = {"promptTokens": 0, "answerTokens": 0, "thoughtsTokens": 0,
              "outputTokens": 0, "cost": 0.0}
    done_chunks = 0
    started = time.time()
    chunk_log: list[dict] = []
    merged_log: list[dict] | None = [] if diag_out else None
    overruns: list[dict] = []

    try:
        for i in range(total_chunks):
            if cancel and cancel():
                raise Cancelled()

            lo = clip_lo + i * chunk_sec
            hi = min(clip_hi, lo + chunk_sec)
            a = max(0, int(lo * RATE) - (over if i else 0))
            b = min(samples.size, int(hi * RATE) + over)
            offset = a / RATE

            notify(phase="transcribe", chunk=i + 1, chunks=total_chunks,
                   done=done_chunks, cost=round(totals["cost"], 5),
                   message=f"{i + 1}/{total_chunks} 本目を文字起こし中"
                           f"（{_mmss(lo)}〜{_mmss(hi)}）")

            carry = [{"speaker": s["speaker"], "text": s["text"]}
                     for s in all_segs[-CARRY_SEGMENTS:]]
            prompt = build_prompt(
                speakers=speakers, num_speakers=num_speakers,
                terms=terms, note=note, carry=carry,
                part=(i + 1, total_chunks), style=style)

            segs, usage = _call_chunk(
                key=key, model=model, audio=_wav_bytes(samples[a:b]),
                prompt=prompt, cancel=cancel)

            for k in totals:
                totals[k] += usage.get(k, 0)

            dropped: list[dict] | None = [] if diag_out else None
            taken = _accept(segs, offset=offset, lo=lo, hi=hi,
                            duration=clip_hi,
                            is_last=(i == total_chunks - 1),
                            sent_lo=a / RATE, sent_hi=b / RATE,
                            dropped=dropped)

            # モデルが送った音声より先の時刻を返していないか。返していれば、
            # そのチャンクの時刻は信用できない（TRAC-25 の実測）。
            ends = [float(r.get("end") or 0.0) for r in segs
                    if isinstance(r.get("end"), (int, float))]
            # 変数名は over と分ける。over は上で「重なりのサンプル数」に使っており、
            # ここで秒数を入れると次のチャンクの a / b が float になって
            # samples[a:b] が落ちる（2 本目以降で必ず失敗した）。
            overrun = (max(ends) - (b - a) / RATE) if ends else 0.0
            if overrun > OVERRUN_LIMIT:
                overruns.append({"chunk": i + 1, "over": round(overrun, 1),
                                 "sentSec": round((b - a) / RATE, 1)})
                notify(phase="transcribe", chunk=i + 1, chunks=total_chunks,
                       done=done_chunks, cost=round(totals["cost"], 5),
                       message=f"{i + 1} 本目は時刻が {overrun:.0f} 秒はみ出しています"
                               "（この範囲は時刻がずれている可能性）")
            all_segs.extend(taken)
            done_chunks += 1

            if diag_out:
                chunk_log.append({
                    "chunk": i + 1,
                    "lo": round(lo, 3), "hi": round(hi, 3),
                    "sentFrom": round(a / RATE, 3), "sentTo": round(b / RATE, 3),
                    "offset": round(offset, 3),
                    "isLast": i == total_chunks - 1,
                    # モデルが返したそのままの時刻（offset を足す前）と、
                    # 足したあとの時刻を並べる。どちらでずれているかを見るため。
                    "raw": [{"start": _num(r.get("start")), "end": _num(r.get("end")),
                             "startAbs": _plus(r.get("start"), offset),
                             "endAbs": _plus(r.get("end"), offset),
                             "speaker": clean_speaker(r.get("speaker")),
                             "text": drop_periods(join_ja(str(r.get("text") or "")))}
                            for r in segs],
                    "acceptedCount": len(taken),
                    "dropped": dropped or [],
                    "usage": {k: usage.get(k) for k in
                              ("promptTokens", "answerTokens", "thoughtsTokens",
                               "outputTokens", "cost")},
                })
    except Cancelled:
        res = _result(all_segs, totals, model, clip_hi - clip_lo, done_chunks,
                      total_chunks, started, chunk_sec, merged_log, overruns)
        # 途中で止めても、そこまでの記録は残す。課金は済んでいる。
        _write_diag(diag_out, video, res, chunk_log, merged_log,
                    clip=(clip_lo, clip_hi))
        raise CancelledWithUsage(res)

    res = _result(all_segs, totals, model, clip_hi - clip_lo, done_chunks,
                  total_chunks, started, chunk_sec, merged_log, overruns)
    _write_diag(diag_out, video, res, chunk_log, merged_log,
                clip=(clip_lo, clip_hi))
    return res


def _num(v) -> float | None:
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


def _plus(v, offset: float) -> float | None:
    n = _num(v)
    return None if n is None else round(n + offset, 3)


def _write_diag(diag_out: Path | None, video: Path, res: dict,
                chunk_log: list[dict], merged: list[dict] | None = None,
                clip: tuple[float, float] | None = None) -> None:
    """生の記録を書く。ここが失敗しても字幕は返す。"""
    if not diag_out:
        return
    doc = {
        "version": DIAG_VERSION,
        "video": video.name,
        "model": res["model"],
        # 流した範囲（元動画の時刻）。duration はこの範囲の長さ。
        "clipFrom": round(clip[0], 3) if clip else 0.0,
        "clipTo": round(clip[1], 3) if clip else res["duration"],
        "chunkSec": res["chunkSec"],
        "duration": res["duration"],
        "chunks": res["chunks"],
        "chunksDone": res["chunksDone"],
        "overlapSec": OVERLAP_SEC,
        "elapsed": res["elapsed"],
        "at": time.time(),
        "usage": res["usage"],
        "acceptedTotal": len(res["segments"]),
        "merged": merged or [],
        "overruns": res.get("overruns", []),
        "chunkList": chunk_log,
        # 採用して重複整理まで済んだ最終形。正解との突き合わせはこれで行う。
        "segments": res["segments"],
    }
    try:
        diag_out.parent.mkdir(parents=True, exist_ok=True)
        tmp = diag_out.with_suffix(diag_out.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(diag_out)
    except OSError:
        pass


class CancelledWithUsage(Cancelled):
    """途中で止めたときに、そこまでの結果と使った額を持って上がる。"""

    def __init__(self, result: dict):
        super().__init__("中止しました")
        self.result = result


def _mmss(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 60:d}:{t % 60:02d}"


def _result(segs, totals, model, duration, done, total, started, chunk_sec,
            merged: list[dict] | None = None,
            overruns: list[dict] | None = None) -> dict:
    return {
        "segments": _dedupe(list(segs), merged),
        "overruns": overruns or [],
        "model": model,
        "duration": round(duration, 3),
        "chunkSec": chunk_sec,
        "chunksDone": done,
        "chunks": total,
        "elapsed": round(time.time() - started, 1),
        "usage": {
            "promptTokens": totals["promptTokens"],
            "answerTokens": totals["answerTokens"],
            "thoughtsTokens": totals["thoughtsTokens"],
            "outputTokens": totals["outputTokens"],
            "cost": round(totals["cost"], 6),
            "pricePerMTok": price_of(model),
        },
    }
