"""文字起こしに API をどれだけ使ったかの記録。

    ~/.traccia/usage.jsonl     1 行 = 文字起こし 1 回

ここに出る金額は**手元の見積もり**であって請求書ではない。応答に入っている
実測トークン数に、こちらが持っている単価表を掛けたもの。単価は変わるし、
無料枠や割引はここに反映されない。実費は Cloud の請求画面が正本。

それでも記録する理由は、請求画面が「どの動画にいくらかかったか」を教えて
くれないため。反映も 1 日ほど遅れるので、押した直後に効くブレーキが要る。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import config

# 実費を確認する場所。画面にそのまま出す。
CONSOLES = [
    {
        "name": "Cloud 請求（実費）",
        "url": "https://console.cloud.google.com/billing",
        "note": "「レポート」でサービスを Generative Language API に絞る。反映は 1 日ほど遅れる",
    },
    {
        "name": "AI Studio（使用量）",
        "url": "https://aistudio.google.com/usage",
        "note": "リクエスト数とトークン数、レート制限の消化状況。金額は Cloud 側が正本",
    },
    {
        "name": "予算とアラート",
        "url": "https://console.cloud.google.com/billing/budgets",
        "note": "使いすぎを止めるならここ。あとから見るより先に上限を張るほうが安全",
    },
]


def _month(ts: float) -> str:
    return time.strftime("%Y-%m", time.localtime(ts))


def record(entry: dict) -> dict:
    """1 行追記する。記録に失敗しても呼び出し元は止めない。"""
    row = dict(entry)
    row.setdefault("at", time.time())
    row["month"] = _month(row["at"])
    try:
        d = config.home()
        d.mkdir(parents=True, exist_ok=True)
        with config.usage_file().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return row


def rows() -> list[dict]:
    f: Path = config.usage_file()
    if not f.exists():
        return []
    out: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue          # 途中で壊れた行は飛ばす
            if isinstance(d, dict):
                out.append(d)
    except OSError:
        return []
    return out


def summary(limit: int = 20) -> dict:
    """累積・今月・直近の一覧。画面の「これまでにいくら使ったか」はここから出す。"""
    all_rows = rows()
    now_month = _month(time.time())

    total = sum(float(r.get("cost") or 0.0) for r in all_rows)
    month = sum(float(r.get("cost") or 0.0)
                for r in all_rows if r.get("month") == now_month)
    month_rows = [r for r in all_rows if r.get("month") == now_month]

    recent = sorted(all_rows, key=lambda r: float(r.get("at") or 0), reverse=True)[:limit]

    return {
        "totalCost": round(total, 6),
        "totalCount": len(all_rows),
        "monthCost": round(month, 6),
        "monthCount": len(month_rows),
        "month": now_month,
        "recent": recent,
        "path": str(config.usage_file()),
        "consoles": CONSOLES,
    }


def month_cost() -> float:
    """今月ぶんの累積。月次上限の判定に使う。"""
    now_month = _month(time.time())
    return sum(float(r.get("cost") or 0.0)
               for r in rows() if r.get("month") == now_month)
