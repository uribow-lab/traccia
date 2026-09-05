"""この PC の設定。素材ではなく人に紐づくもの（API キーなど）を置く。

    ~/.traccia/config.json     API キー・使う / 使わない・モデル・上限
    ~/.traccia/usage.jsonl     文字起こし 1 回ぶんの実測トークンと概算額

素材フォルダに置かないのは、素材ごと誰かに渡したときにキーまで一緒に
渡ってしまうため。ファイルは本人だけが読める権限（0600）で書く。

環境変数 GEMINI_API_KEY があれば、保存されたキーが無いときの控えとして使う。
bench/ の実験と同じキーをそのまま使えるようにするため。
TRACCIA_HOME を指定すると置き場所を変えられる（検証用）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_VERSION = 1

# 福よし 10 分（正解 310 行）で 3.6 / 3.7 / 3.8 を実測して選んだ。3.7 は
# 話者の一致 77%（3.6 は 52% で、全部を最頻話者に振ったのと同じ）、
# 開始のずれ中央 -0.09 秒（3.6 は -0.70 秒、最悪 -15.6 秒）、
# しかも 3.6 の半額で 2.6 倍速い。詳しくは docs/api-keys.md。
DEFAULT_MODEL = "gemini-3.7-flash"

# 1 回の要求に載せる音声の長さ（秒）。
# 長いほど話者の取り違えが減り要求回数も減るが、1 回が重くなる。
# 16kHz mono の WAV で 240 秒 ≒ 7.7MB。base64 にして約 10MB で、
# インライン送信の上限（要求全体で 20MB）に対して余裕がある。
# 1 回に送る秒数。長いほどモデルの時計が狂いやすい。240 秒では 8 本中 4 本で
# 時刻が 40 秒飛んだが、60 秒では後ろへのずれが 117 件 → 1 件になった（TRAC-25）。
#
# 3.7 Flash でも同じだった。福よし 10 分・正解 310 行で 60 / 120 / 240 を比べると、
# 中央のずれはどれも -0.2 秒以内で差が無いのに、**最後の 1 本だけが丸ごと
# 40 秒後ろへ飛ぶ**。120 秒でも 240 秒でも起きる（どちらも 126 秒ぶんの音を
# 送った本で発生）。中央値では見えないので、必ず外れ値のほうを見ること。
#
#             60 秒     120 秒    240 秒
#   ずれ中央  -0.08s    -0.17s    -0.04s   ← 差が無い
#   最悪(後)  +2.65s   +40.81s   +40.55s   ← ここで差が出る
DEFAULT_CHUNK_SEC = 60
MIN_CHUNK_SEC = 30
MAX_CHUNK_SEC = 300


def home() -> Path:
    raw = os.environ.get("TRACCIA_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".traccia"


def config_file() -> Path:
    return home() / "config.json"


def usage_file() -> Path:
    return home() / "usage.jsonl"


def _read() -> dict:
    f = config_file()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        # 壊れていても起動は止めない。次の保存で書き直される。
        return {}
    return data if isinstance(data, dict) else {}


# 以前の既定。この値のまま保存されているものは「選んだ値」ではなく
# 配っていた既定なので、新しい既定へ移す（下の _migrate_chunk を参照）。
LEGACY_CHUNK_SEC = 240


def _migrate_chunk(v) -> int:
    """保存済みの 240 秒を 60 秒へ移す。

    240 秒では 8 本中 4 本で時刻が 40 秒飛び、出力の一部が失われていた
    （TRAC-25 の実測）。この値は利用者が選んだものではなく、こちらが
    既定として配っていたものなので、黙って残すほうが害が大きい。

    240 以外の値（180 や 300 など）は、そのまま尊重する。
    """
    if v is None:
        return DEFAULT_CHUNK_SEC
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_CHUNK_SEC
    return DEFAULT_CHUNK_SEC if n == LEGACY_CHUNK_SEC else n


def _clamp_chunk(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_CHUNK_SEC
    return max(MIN_CHUNK_SEC, min(MAX_CHUNK_SEC, n))


def load() -> dict:
    """既定で埋めた設定を返す。キーの実体を含むのでそのまま画面に出さないこと。"""
    raw = _read()
    g = raw.get("gemini") if isinstance(raw.get("gemini"), dict) else {}
    try:
        limit = float(g.get("monthlyLimitUsd") or 0.0)
    except (TypeError, ValueError):
        limit = 0.0
    return {
        "version": CONFIG_VERSION,
        "gemini": {
            "apiKey": str(g.get("apiKey") or ""),
            # 既定は「使わない」。キーを入れただけでは動かさない。
            "enabled": bool(g.get("enabled")),
            "model": str(g.get("model") or DEFAULT_MODEL),
            # 0 なら上限なし。今月の累積がここを超えていたら実行を止める。
            "monthlyLimitUsd": max(0.0, limit),
            "chunkSec": _clamp_chunk(_migrate_chunk(g.get("chunkSec"))),
        },
    }


def save(payload: dict) -> dict:
    """画面から来た設定を書く。送られてこなかった項目は今の値を残す。

    apiKey は空文字なら「変更なし」とみなす。画面にはマスクした文字列しか
    出していないので、それを送り返されてキーが壊れるのを防ぐ。
    消したいときは clearKey: true を送る。
    """
    cur = load()["gemini"]
    g = payload.get("gemini") if isinstance(payload.get("gemini"), dict) else payload
    g = g if isinstance(g, dict) else {}

    key = cur["apiKey"]
    if g.get("clearKey"):
        key = ""
    else:
        incoming = str(g.get("apiKey") or "").strip()
        if incoming and "…" not in incoming:
            key = incoming

    try:
        limit = float(g.get("monthlyLimitUsd", cur["monthlyLimitUsd"]) or 0.0)
    except (TypeError, ValueError):
        limit = cur["monthlyLimitUsd"]

    doc = {
        "version": CONFIG_VERSION,
        "gemini": {
            "apiKey": key,
            "enabled": bool(g.get("enabled", cur["enabled"])),
            "model": str(g.get("model") or cur["model"]),
            "monthlyLimitUsd": max(0.0, limit),
            "chunkSec": _clamp_chunk(g.get("chunkSec", cur["chunkSec"])),
        },
    }

    d = home()
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass

    f = config_file()
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(f)
    return doc


def resolved_key() -> tuple[str, str]:
    """使うキーと、その出どころ。("", "") なら未設定。"""
    key = load()["gemini"]["apiKey"]
    if key:
        return key, "config"
    env = os.environ.get("GEMINI_API_KEY", "").strip()
    if env:
        return env, "env"
    return "", ""


def mask(key: str) -> str:
    """画面に出すための伏せ字。頭と尻だけ残して同一性を確認できるようにする。"""
    if not key:
        return ""
    if len(key) <= 12:
        return key[:2] + "…"
    return f"{key[:6]}…{key[-4:]}"


def public() -> dict:
    """画面に返してよい形。キーの実体は含めない。"""
    g = load()["gemini"]
    key, source = resolved_key()
    return {
        "gemini": {
            "hasKey": bool(key),
            "keyMasked": mask(key),
            "keySource": source,          # config / env / ""
            "enabled": bool(g["enabled"]),
            "model": g["model"],
            "monthlyLimitUsd": g["monthlyLimitUsd"],
            "chunkSec": g["chunkSec"],
        },
        "configPath": str(config_file()),
    }
