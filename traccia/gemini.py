"""Gemini に音声を渡して、話者付きの字幕を作る。

bench/gemini.py の実験を、1 本まるごとの動画で回せる形にしたもの。
違いは 3 つ。

  ・動画から音声を取り出して一定長に切る。1 時間ぶんを 1 回では送れない
  ・チャンクをまたいで話者が入れ替わらないよう、直前の発話を一緒に渡す
  ・実測トークンを 1 回ずつ記録して、いくら使ったかが後から見えるようにする

専用 ASR（Deepgram など）と違って、Gemini には「この語が出てくる」「何人の
会話だ」と教えられる。店名や地名のような耳慣れない語はそこで効く。
そのぶん高い（3.6 Flash の実測で 1 時間あたり $2 前後）ので、
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
# 注意: 思考トークン（thoughtsTokenCount）も出力として課金される。
# 実測では出力の 7 割強を思考が占めた。本文の長さだけで見積もると大きく外す。
PRICING = {
    "gemini-3.6-flash":      {"in": 1.50, "out": 7.50},
    "gemini-3.5-flash-lite": {"in": 0.50, "out": 3.00},
    "gemini-3-flash":        {"in": 1.00, "out": 6.00},
}
PRICING_FALLBACK = {"in": 1.50, "out": 7.50}

# 画面のモデル選択に出す候補。ここに無いモデルでも設定に直接書けば通る。
MODEL_CHOICES = [
    {"id": "gemini-3.6-flash",      "label": "3.6 Flash（既定・精度と値段の釣り合いがよい）"},
    {"id": "gemini-3.5-flash-lite", "label": "3.5 Flash Lite（安い・取りこぼしは増える）"},
    {"id": "gemini-3-flash",        "label": "3 Flash（preview）"},
]

# 見積もりに使う 1 秒あたりのトークン数。
#   入力 … 音声は 1 秒 ≒ 32 トークン（Gemini の数え方）
#   出力 … 45 秒のクリップの実測から逆算した値。7 割強が思考トークン。
#           発話の密度で振れるので、あくまで目安。
AUDIO_TOKENS_PER_SEC = 32
OUTPUT_TOKENS_PER_SEC = 68


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
    out_tok = duration * OUTPUT_TOKENS_PER_SEC
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

PROMPT = """この音声を書き起こして、字幕として使える形で返してください。

条件:
- 日本語。分かち書きしない（語の間に空白を入れない）
- 1 区間は 1 発話。相槌や短い返事も 1 区間として拾う
- 区間は 1〜5 秒程度。長い発話は意味の切れ目で分ける
- 話者が変わったら必ず区間を分ける
- 聞き取れない箇所は無理に埋めず、その区間を省く
- 時刻はこの音声の先頭を 0.0 秒とした秒数（小数第 2 位まで）
- speaker には「A」「B」のように、名前だけを入れる（「話者A」とは書かない）
"""


def build_prompt(*, speakers: list[str] | None, num_speakers: int | None,
                 terms: list[str] | None, note: str | None,
                 carry: list[dict] | None, part: tuple[int, int] | None) -> str:
    p = PROMPT

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
            duration: float, is_last: bool) -> list[dict]:
    """このチャンクが担当する範囲の区間だけを拾う。

    前後に余分な音を付けて送っているので、境界の発話は隣のチャンクにも
    現れる。区間の**中心**が担当範囲に入るものだけ採ることで、
    どちらか一方にだけ残る。またぐ発話は、それを最後まで聞けた側が採る。
    """
    out = []
    for s in segs:
        try:
            start = float(s["start"]) + offset
            end = float(s["end"]) + offset
        except (KeyError, TypeError, ValueError):
            continue
        # join_ja が先。逆にすると、「。」の代わりに入れた半角スペースを
        # join_ja が「日本語の間の空白」とみなして消してしまう。
        text = drop_periods(join_ja(str(s.get("text") or "")))
        if not text:
            continue

        # モデルは音声の外の時刻を返すことがある。先に音声の中へ収める。
        # ここを通さないと開始が終了より後ろの区間ができて、字幕として壊れる。
        start = max(0.0, start)
        if start >= duration:
            continue
        end = min(duration, max(end, start))
        if end - start < 0.15:
            end = min(duration, start + 0.4)
        if end <= start:
            continue

        center = (start + end) / 2
        if center < lo or (center >= hi and not is_last):
            continue
        out.append({"start": start, "end": end,
                    "speaker": clean_speaker(s.get("speaker")), "text": text})
    return out


def _dedupe(segs: list[dict]) -> list[dict]:
    """同じ文言が同じ場所に二重に入っているものを落とす。

    中心での振り分けで大半は防げるが、モデルが同じ発話を少しずれた時刻で
    返すことがあるので、最後にもう一度だけ見る。
    """
    segs.sort(key=lambda s: (s["start"], s["end"]))
    out: list[dict] = []
    for s in segs:
        dup = next((p for p in reversed(out[-6:])
                    if p["text"] == s["text"] and abs(p["start"] - s["start"]) < 2.0), None)
        if dup:
            dup["end"] = max(dup["end"], s["end"])
            continue
        out.append(s)
    return out


def transcribe(video: Path, *, key: str, model: str, chunk_sec: int,
               terms: list[str] | None = None, note: str = "",
               speakers: list[str] | None = None, num_speakers: int | None = None,
               peaks_out: Path | None = None,
               progress=None, cancel=None) -> dict:
    """動画 1 本を書き起こす。戻り値の segments はそのまま字幕にできる。

    progress(dict) は段階ごとに呼ばれる。cancel() が True を返したら
    Cancelled を送出して途中で降りる（そこまでに使った額は返り値に残す）。
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

    if peaks_out:
        notify(phase="audio", message="波形を作っています…")
        write_peaks(samples, peaks_out)

    if cancel and cancel():
        raise Cancelled()

    chunk_sec = max(30, int(chunk_sec))
    total_chunks = max(1, int(-(-duration // chunk_sec)))
    over = int(OVERLAP_SEC * RATE)

    all_segs: list[dict] = []
    totals = {"promptTokens": 0, "answerTokens": 0, "thoughtsTokens": 0,
              "outputTokens": 0, "cost": 0.0}
    done_chunks = 0
    started = time.time()

    try:
        for i in range(total_chunks):
            if cancel and cancel():
                raise Cancelled()

            lo = i * chunk_sec
            hi = min(duration, lo + chunk_sec)
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
                part=(i + 1, total_chunks))

            segs, usage = _call_chunk(
                key=key, model=model, audio=_wav_bytes(samples[a:b]),
                prompt=prompt, cancel=cancel)

            for k in totals:
                totals[k] += usage.get(k, 0)

            all_segs.extend(_accept(segs, offset=offset, lo=lo, hi=hi,
                                    duration=duration,
                                    is_last=(i == total_chunks - 1)))
            done_chunks += 1
    except Cancelled:
        raise CancelledWithUsage(_result(all_segs, totals, model, duration,
                                         done_chunks, total_chunks, started,
                                         chunk_sec))

    return _result(all_segs, totals, model, duration, done_chunks, total_chunks,
                   started, chunk_sec)


class CancelledWithUsage(Cancelled):
    """途中で止めたときに、そこまでの結果と使った額を持って上がる。"""

    def __init__(self, result: dict):
        super().__init__("中止しました")
        self.result = result


def _mmss(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 60:d}:{t % 60:02d}"


def _result(segs, totals, model, duration, done, total, started, chunk_sec) -> dict:
    return {
        "segments": _dedupe(list(segs)),
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
