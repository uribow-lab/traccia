"""音声波形のピーク列を作る・読む・書く。

タイムラインに敷く波形の元データ。1/100 秒ごとの振幅を 0–255 で持つだけの
軽いファイルで、484 秒の素材でも 200KB 程度に収まる。

このモジュールはパッケージ内の相対 import を使わない。
transcribe.py が話者分離のワーカーとして単体スクリプトのまま起動されるため、
そこからも同じコードを使えるようにしておく必要がある。

ピークファイル: <セット>/<名前>.peaks.json
{
  "version": 1,
  "duration": 484.9,     秒
  "rate": 100,           1 秒あたりのピーク数
  "peaks": [0..255, ...] 長さ = ceil(duration * rate)
}
"""

from __future__ import annotations

import json
import math
from pathlib import Path

PEAKS_VERSION = 1
DEFAULT_RATE = 100      # peaks / sec
DECODE_RATE = 8000      # 波形を見るだけなら 8kHz で十分（1 バケット 80 サンプル）

# 1 発の破裂音で全体が潰れないよう、最大値ではなくこの分位点を振り切り値にする
NORMALIZE_PERCENTILE = 99.5


class PeaksError(Exception):
    pass


class NotExtracted(Exception):
    """まだ抽出されていない。"""


def build(samples, sample_rate: int, rate: int = DEFAULT_RATE, duration: float | None = None) -> dict:
    """mono の float サンプル列からピーク列を作る。

    samples は numpy 配列（float32 想定、範囲はおよそ -1..1）。
    """
    try:
        import numpy as np
    except ImportError as e:  # pragma: no cover
        raise PeaksError("numpy が必要です（requirements.txt を入れてください）") from e

    samples = np.asarray(samples, dtype="float32").reshape(-1)
    if samples.size == 0:
        raise PeaksError("音声が空です")

    per = max(1, int(round(sample_rate / rate)))          # 1 バケットのサンプル数
    count = int(math.ceil(samples.size / per))
    pad = count * per - samples.size
    if pad:
        samples = np.concatenate([samples, np.zeros(pad, dtype="float32")])

    buckets = np.abs(samples.reshape(count, per)).max(axis=1)

    ref = float(np.percentile(buckets, NORMALIZE_PERCENTILE))
    if ref <= 1e-6:
        ref = float(buckets.max()) or 1.0
    vals = np.clip(buckets / ref, 0.0, 1.0) * 255.0

    return {
        "version": PEAKS_VERSION,
        "duration": round(duration if duration else samples.size / sample_rate, 3),
        "rate": rate,
        "peaks": [int(v) for v in vals.astype("uint8")],
    }


def decode_mono(path: str | Path, rate: int = DECODE_RATE):
    """PyAV で音声を mono float32 に落とす。動画ファイルでもよい。"""
    try:
        import av
        import numpy as np
    except ImportError as e:
        raise PeaksError("PyAV と numpy が必要です（requirements.txt を入れてください）") from e

    container = av.open(str(path))
    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise PeaksError("音声トラックがありません")
        resampler = av.audio.resampler.AudioResampler(format="flt", layout="mono", rate=rate)
        chunks = []
        for frame in container.decode(stream):
            for rframe in resampler.resample(frame):
                chunks.append(rframe.to_ndarray().reshape(-1))
    finally:
        container.close()

    if not chunks:
        raise PeaksError("音声を読み取れませんでした")
    return np.concatenate(chunks).astype("float32")


def extract(media: str | Path, rate: int = DEFAULT_RATE) -> dict:
    """動画・音声ファイルから直接ピーク列を作る（デコードを含む）。"""
    samples = decode_mono(media)
    return build(samples, DECODE_RATE, rate=rate)


def save(peaks_file: str | Path, data: dict) -> None:
    peaks_file = Path(peaks_file)
    peaks_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = peaks_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(peaks_file)


def load(peaks_file: str | Path) -> dict:
    peaks_file = Path(peaks_file)
    if not peaks_file.exists():
        raise NotExtracted(str(peaks_file))
    data = json.loads(peaks_file.read_text(encoding="utf-8"))
    if data.get("version") != PEAKS_VERSION:
        raise NotExtracted(f"未対応の peaks version: {data.get('version')}")
    return data
