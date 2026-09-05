"""ローカルの文字起こし（faster-whisper）を、エディタから呼べる形にしたもの。

transcribe.py はコマンドとして完結した作りで、ファイルを受け取って .srt を書く。
こちらは **すでにメモリにある音声**を受け取って区間を返すだけの薄い層で、
Gemini と同じ 1 回のデコードを使い回すために分けてある。

なぜローカルを使うのか
----------------------
Gemini の時刻はモデルの推定で、チャンクの中で 40 秒飛ぶことがある。
Whisper の時刻は音声そのものから出るので飛ばない。実測（29 分の素材・確定版 734 件）:

    Gemini 60 秒    ±2 秒に収まる行 79%   中央 0.77s   最悪 -20.2s
    ローカル small   ±2 秒に収まる行 95%   中央 0.40s   最悪 -25.6s
    ローカル medium  ±2 秒に収まる行 95%   中央 0.60s   最悪 -13.8s

本文は Gemini が良く、時刻はローカルが良い。両方を使う（traccia/merge.py）。

小さいモデルほど時刻が細かく、大きいモデルほど拾う数が多い。だから
small と medium は「どちらが上」ではなく、役割が違う。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# 精度の既定は device で決める。GPU の int8 は出力が壊れる（TRAC-27）。
from .transcribe import (REPEAT_LIMIT, REPEAT_SHARE, default_compute_type,
                         drop_periods)

RATE = 16000            # 受け取る音声のサンプリングレート（gemini.load_audio と同じ）

# 画面に出す選択肢。所要は Intel i7-7700K（CPU・int8）で 29 分の素材を流した実測。
MODEL_CHOICES = [
    {"id": "small",  "label": "small（時刻が細かい）",   "secPerSec": 934 / 1737},
    {"id": "medium", "label": "medium（拾う数が多い）", "secPerSec": 1359 / 1737},
]


class LocalError(Exception):
    """呼び出し側に見せるエラー。文面はそのまま画面に出る。"""


class Cancelled(Exception):
    """利用者が途中で止めた。"""


@dataclass
class Result:
    model: str
    segments: list[dict] = field(default_factory=list)
    duration: float = 0.0
    elapsed: float = 0.0
    device: str = ""
    computeType: str = ""
    repeated: int = 0          # 同じ本文が続いた最大回数。暴走の指標
    warning: str = ""

    def to_dict(self) -> dict:
        return {
            "model": self.model, "duration": round(self.duration, 3),
            "elapsed": round(self.elapsed, 1), "device": self.device,
            "computeType": self.computeType, "count": len(self.segments),
            "repeated": self.repeated, "warning": self.warning,
        }


def available() -> bool:
    """この環境でローカル文字起こしが使えるか。"""
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def estimate_sec(duration: float, model: str) -> float:
    """流す前に出す所要時間の目安。実測から線形に見る。"""
    per = next((m["secPerSec"] for m in MODEL_CHOICES if m["id"] == model), 0.6)
    return duration * per


def pick_device(prefer: str = "auto") -> tuple[str, str]:
    """使うデバイスと計算精度を決める。"""
    device = prefer
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:  # noqa: BLE001
            device = "cpu"
    return device, default_compute_type(device)


def transcribe(samples, *, model: str = "small", language: str = "ja",
               terms: list[str] | None = None, note: str = "",
               device: str = "auto", progress=None, cancel=None) -> Result:
    """すでにデコード済みの音声を書き起こす。

    samples は int16 の numpy 配列（16kHz mono）。gemini.load_audio() の戻り値を
    そのまま渡せる。動画を読み直さないので、2.2GB の素材でも待たされない。

    progress(dict) は区間が 1 つ返るたびに呼ばれる。at（いま何秒まで進んだか）と
    duration を持つので、割合はそこから出す。cancel() が True を返したら
    Cancelled を送出して途中で降りる。
    """
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise LocalError("faster-whisper が入っていません"
                         "（requirements.txt を入れてください）") from e

    duration = len(samples) / RATE
    if duration < 0.2:
        raise LocalError("音声が短すぎます")

    dev, compute = pick_device(device)
    audio = np.asarray(samples).astype("float32") / 32768.0

    started = time.time()
    if progress:
        progress({"phase": "load", "at": 0.0, "duration": duration,
                  "message": f"{model} を読み込んでいます…"})
    try:
        engine = WhisperModel(model, device=dev, compute_type=compute)
    except Exception as e:  # noqa: BLE001
        raise LocalError(f"モデル '{model}' を読み込めません（{dev}/{compute}）: {e}") from e

    hotwords = "、".join(terms) if terms else None
    initial_prompt = (note if note else None)

    try:
        segments, _info = engine.transcribe(
            audio,
            language=None if language == "auto" else language,
            beam_size=5,
            vad_filter=True,
            hotwords=hotwords,
            initial_prompt=initial_prompt,
        )
    except Exception as e:  # noqa: BLE001
        raise LocalError(f"文字起こしを開始できません: {e}") from e

    out: list[dict] = []
    for seg in segments:
        if cancel and cancel():
            raise Cancelled()
        text = drop_periods(seg.text).strip()
        if not text:
            continue
        out.append({"start": float(seg.start), "end": float(seg.end),
                    "speaker": "", "text": text})
        if progress:
            progress({"phase": "transcribe", "at": float(seg.end),
                      "duration": duration, "count": len(out), "text": text})

    res = Result(model=model, segments=out, duration=duration,
                 elapsed=time.time() - started, device=dev, computeType=compute)
    res.repeated, res.warning = _check_repetition(out, dev, compute)
    return res


def _check_repetition(segs: list[dict], device: str, compute: str) -> tuple[int, str]:
    """同じ本文が続いていないか見る。暴走しても .srt は正常に書けてしまうため。

    判定そのものは transcribe.py と同じ考え方。ここでは Line ではなく dict を
    扱うので、数えるところだけ持っている。
    """
    best, i = (0, 0, 0), 0
    while i < len(segs):
        j = i
        while j + 1 < len(segs) and segs[j + 1]["text"] == segs[i]["text"]:
            j += 1
        if j - i + 1 > best[0]:
            best = (j - i + 1, i, j)
        i = j + 1

    n, a, b = best
    if n <= REPEAT_LIMIT:
        return n, ""

    span = segs[b]["end"] - segs[a]["start"]
    total = (segs[-1]["end"] - segs[0]["start"]) if segs else 0
    share = span / total if total > 0 else 0.0

    # 回数だけでは足りない。「うん」が 7 回続いても、それが出力の 1% なら暴走では
    # ない。実際に 7 回・1% で「暴走しています」と出して、警告が信用を失った。
    if share < REPEAT_SHARE:
        return n, ""

    hint = ("GPU の int8 で起きやすい問題です。計算精度を float32 にしてください"
            if device == "cuda" and compute.startswith("int8")
            else "モデルか計算精度を変えて試してください")
    return n, (f"同じ本文が {n} 回続いています（出力の {share * 100:.0f}%）。"
               f"文字起こしが暴走しています。{hint}")
