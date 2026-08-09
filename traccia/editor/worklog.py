"""編集にかけた時間の記録。

「20 分の動画・字幕 437 件・話者 4 人で、どのくらいかかるのか」を後から
引けるようにするためのもの。次の仕事を見積もるときの材料になる。

    <セット>/<名前>.worklog.json

素材フォルダに置くので、Windows と Mac でフォルダごと移しても続きが取れる。

計測そのものはブラウザ側で行う。マウスやキーが動いたかどうかはサーバーから
見えないため。ここは送られてきた差分を足して残すだけ。

同じセットを 2 つのタブで開くと二重に足されるので、セットごとに「いま計測して
いるタブ」を 1 つだけ通す（Leases を参照）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .project import SetPaths, list_sets

WORKLOG_VERSION = 1

# 前回の記録からこれだけ空いたら、別の作業回として分けて記録する。
# 通しで何時間やったかと、何回に分けてやったかの両方を見たいため。
SESSION_GAP_SEC = 600

# 記録として残す作業回の数。古いものから捨てる。合計には影響しない。
KEEP_SESSIONS = 200

# 計測しているタブが黙ってからこれだけ過ぎたら、別のタブに譲る。
# ブラウザを落としたまま戻ってこない場合に、いつまでも計測できなくなるのを防ぐ。
LEASE_TTL_SEC = 90


def _empty(paths: SetPaths) -> dict:
    return {
        "version": WORKLOG_VERSION,
        "name": paths.name,
        "stem": paths.stem,
        "totalActiveSec": 0.0,
        "firstAt": None,
        "lastAt": None,
        "sessions": [],
        "snapshot": {},
    }


def load(paths: SetPaths) -> dict:
    f = paths.worklog_file
    if not f.exists():
        return _empty(paths)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        # 壊れていても編集は続けられるべきなので、空として扱う。
        # 次の保存で書き直される（元は backup/ に退避される）。
        return _empty(paths)
    if not isinstance(data, dict):
        return _empty(paths)
    base = _empty(paths)
    base.update({k: v for k, v in data.items() if k in base})
    base["sessions"] = [s for s in (data.get("sessions") or []) if isinstance(s, dict)]
    base["snapshot"] = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
    try:
        base["totalActiveSec"] = float(base["totalActiveSec"] or 0.0)
    except (TypeError, ValueError):
        base["totalActiveSec"] = 0.0
    return base


def _write(paths: SetPaths, doc: dict) -> None:
    tmp = paths.worklog_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(paths.worklog_file)


def clean_snapshot(raw) -> dict:
    """画面から来た「そのときの動画長・字幕数・話者数」を整える。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    try:
        d = float(raw.get("durationSec") or 0.0)
        if d > 0:
            out["durationSec"] = round(d, 3)
    except (TypeError, ValueError):
        pass
    for key in ("cues", "speakers"):
        # キーが無いときは触らない。前に記録した値を 0 で潰さないため
        if raw.get(key) is None:
            continue
        try:
            v = int(raw[key])
        except (TypeError, ValueError):
            continue
        if v >= 0:
            out[key] = v
    return out


def add(paths: SetPaths, *, session_id: str, add_sec: float, snapshot=None) -> dict:
    """作業時間の差分を足す。

    累積ではなく差分を受け取るのは、送信を取りこぼしても二重にならないため。
    """
    try:
        add_sec = float(add_sec)
    except (TypeError, ValueError):
        add_sec = 0.0
    # 1 回の送信で足せる上限。時計のずれや細工で跳ね上がるのを防ぐ。
    add_sec = max(0.0, min(add_sec, 3600.0))

    doc = load(paths)
    now = time.time()

    snap = clean_snapshot(snapshot)
    if snap:
        # 最後に見た状態で上書きする。字幕は増えていくので、最新が実態に近い
        doc["snapshot"] = {**doc.get("snapshot", {}), **snap}

    if add_sec > 0:
        doc["totalActiveSec"] = round(float(doc["totalActiveSec"]) + add_sec, 1)
        doc["firstAt"] = doc.get("firstAt") or now
        doc["lastAt"] = now

        sessions = doc["sessions"]
        last = sessions[-1] if sessions else None
        same_run = (
            last is not None
            and last.get("session") == session_id
            and now - float(last.get("until") or 0) < SESSION_GAP_SEC
        )
        if same_run:
            last["activeSec"] = round(float(last.get("activeSec") or 0) + add_sec, 1)
            last["until"] = now
        else:
            sessions.append({
                "session": session_id,
                "at": now,
                "until": now,
                "activeSec": round(add_sec, 1),
            })
        if len(sessions) > KEEP_SESSIONS:
            del sessions[:-KEEP_SESSIONS]

    _write(paths, doc)
    return doc


class Leases:
    """セットごとに「いま計測しているタブ」を 1 つだけ通す。

    同じセットを 2 つのタブで開いたときに、両方が同じ時間を足して
    倍になるのを防ぐ。先に来たタブが持ち、黙ったら次のタブへ移る。
    """

    def __init__(self, ttl: float = LEASE_TTL_SEC):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._held: dict[str, tuple[str, float]] = {}   # セット名 -> (session, 最終受信)

    def claim(self, name: str, session_id: str) -> bool:
        """このタブが計測してよいか。譲れないときは False。"""
        if not session_id:
            return True          # セッションを送ってこない古い画面は素通しする
        now = time.time()
        with self._lock:
            holder = self._held.get(name)
            if holder and holder[0] != session_id and now - holder[1] < self._ttl:
                return False
            self._held[name] = (session_id, now)
            return True

    def holder(self, name: str) -> str | None:
        with self._lock:
            h = self._held.get(name)
            return h[0] if h and time.time() - h[1] < self._ttl else None


def summary(resources: Path) -> dict:
    """全セットを並べた一覧。これが見積もりの目安になる。

    動画 1 分あたり何分かかったかを出しておくと、次の仕事の当たりが付く。
    """
    rows = []
    for s in list_sets(resources):
        doc = load(s)
        snap = doc.get("snapshot") or {}
        active = float(doc.get("totalActiveSec") or 0.0)
        dur = float(snap.get("durationSec") or 0.0)
        rows.append({
            "name": s.name,
            "activeSec": round(active, 1),
            "durationSec": round(dur, 1) if dur else None,
            "cues": snap.get("cues"),
            "speakers": snap.get("speakers"),
            # 動画 1 分あたりの作業時間（分）。これが見積もりに使う数字
            "minPerVideoMin": round((active / 60) / (dur / 60), 2) if dur > 0 and active > 0 else None,
            # 字幕 1 件あたりの作業時間（秒）
            "secPerCue": round(active / snap["cues"], 1) if snap.get("cues") and active > 0 else None,
            "sessions": len(doc.get("sessions") or []),
            "lastAt": doc.get("lastAt"),
            "measured": active > 0,
        })

    measured = [r for r in rows if r["measured"]]
    total_active = sum(r["activeSec"] for r in measured)
    total_dur = sum(r["durationSec"] or 0 for r in measured)
    total_cues = sum(r["cues"] or 0 for r in measured)

    return {
        "rows": rows,
        "totals": {
            "activeSec": round(total_active, 1),
            "durationSec": round(total_dur, 1),
            "cues": total_cues,
            "sets": len(measured),
            # 全体をならした目安。1 本ぶんの偏りを均せる
            "minPerVideoMin": round((total_active / 60) / (total_dur / 60), 2) if total_dur > 0 else None,
            "secPerCue": round(total_active / total_cues, 1) if total_cues else None,
        },
    }
