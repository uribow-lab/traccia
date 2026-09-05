"""診断の入口。

    python -m traccia diag <セットフォルダ> [--chunk 240] [--yes]
    python -m traccia diag <セットフォルダ> --no-run          （測り直すだけ）

生の時刻を <名前>.gemini-raw.json に残し、確定版（edit.json）と突き合わせて
報告を出す。**edit.json / settings.json / 原本の .srt には一切書かない。**

--no-run を付けると Gemini を呼ばずに、すでにある記録だけを測り直す。
1 回の課金で分析を何度でも回せるようにするためのもの。

--model と --start / --dur は、モデルを並べて比べるためのもの。

    python -m traccia diag <フォルダ> --model gemini-3.8-flash --dur 600 --chunk 60 --yes

正解が動画の途中までしか無いなら、その範囲だけを流せば課金もそこで止まる。
どちらかを指定すると生の記録は別名になるので、モデルごとの結果が上書きで
消えることはない（既定のままなら従来と同じ名前）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config, diag, gemini, usage
from .editor import project


def _raw_path(paths, chunk_sec: int | None = None,
              model: str | None = None,
              span: tuple[float, float | None] | None = None) -> Path:
    """チャンク長ごとに別のファイルにする。240 秒と 60 秒を並べて比べるため。

    model と span は、指定されたときだけ名前に足す。既定のまま流したときの
    名前を変えないため（これまでに残した記録を --no-run で読めなくしない）。
    """
    if chunk_sec is None:
        return paths.root / f"{paths.stem}.gemini-raw.json"
    name = f"{paths.stem}.gemini-raw.chunk{chunk_sec}"
    if model:
        name += "." + model.removeprefix("gemini-")
    if span:
        start, dur = span
        name += f".{int(start)}-{'end' if dur is None else int(start + dur)}s"
    return paths.root / f"{name}.json"


def _guard(duration: float, chunk_sec: int, model: str) -> tuple[str, dict]:
    """流してよいかを確かめる。エディタ（jobs.py の _guard）と同じ判断をする。

    duration は実際に流す範囲の長さ。範囲を絞ったなら想定費用もそのぶん減る。
    """
    cfg = config.load()["gemini"]
    if not cfg["enabled"]:
        raise SystemExit("Gemini 文字起こしが「使わない」になっています。"
                         "エディタの設定で「使う」に切り替えてください")
    key, _src = config.resolved_key()
    if not key:
        raise SystemExit("Gemini の API キーが設定されていません")

    est = gemini.estimate(duration, model, chunk_sec)
    limit = float(cfg.get("monthlyLimitUsd") or 0.0)
    if limit > 0:
        spent = usage.month_cost()
        if spent + est["cost"] > limit:
            raise SystemExit(
                f"今月の上限 ${limit:.2f} を超えます"
                f"（今月ここまで ${spent:.2f} ＋ 今回の想定 ${est['cost']:.2f}）")
    return key, est


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m traccia diag",
        description="文字起こしの時刻のずれを測る（edit.json には書かない）",
    )
    ap.add_argument("folder", help="セットフォルダ")
    ap.add_argument("--chunk", type=int, default=None,
                    help="1 回に送る秒数（既定: 設定の値）")
    ap.add_argument("--model", default=None,
                    help="使うモデル（既定: 設定の値）。"
                         "使えるものは bench/gemini_models.py で確認できる")
    ap.add_argument("--start", type=float, default=None, metavar="SEC",
                    help="この秒数から流す（既定: 0）")
    ap.add_argument("--dur", type=float, default=None, metavar="SEC",
                    help="この秒数だけ流す（既定: 最後まで）。"
                         "正解が途中までしか無いときに課金をそこで止める")
    ap.add_argument("--truth", default=None,
                    help="正解にする edit.json（既定: セットの edit.json）")
    ap.add_argument("--raw", default=None, help="生の記録の置き場を明示する")
    ap.add_argument("--hyp", default=None,
                    help="比べる側を明示する（.srt / edit.json も可）。"
                         "ローカル文字起こしの時刻を測るときに使う")
    ap.add_argument("--no-run", action="store_true",
                    help="Gemini を呼ばず、すでにある記録だけを測り直す")
    ap.add_argument("--yes", action="store_true",
                    help="想定費用の確認を省いて流す")
    args = ap.parse_args(argv)

    folder = Path(args.folder).expanduser().resolve()
    paths = project.discover_set(folder)
    if not paths:
        print(f"セットとして読めません（動画が見つかりません）: {folder}", file=sys.stderr)
        return 1

    cfg = config.load()["gemini"]
    chunk_sec = int(args.chunk or cfg["chunkSec"])
    model = args.model or cfg["model"]
    start = float(args.start or 0.0)
    span = (start, args.dur) if (args.start is not None or args.dur is not None) else None
    raw_file = (Path(args.raw) if args.raw
                else _raw_path(paths, chunk_sec, args.model, span))

    truth_file = Path(args.truth) if args.truth else paths.project_file
    if not truth_file.exists():
        print(f"正解にする edit.json がありません: {truth_file}", file=sys.stderr)
        return 1

    if not args.no_run:
        info = project.probe(paths.video)
        if not info.duration:
            print("動画の長さを読み取れませんでした（PyAV が必要です）", file=sys.stderr)
            return 1
        if start >= info.duration:
            print(f"--start が動画の長さ（{info.duration:.0f} 秒）を超えています",
                  file=sys.stderr)
            return 1
        span_sec = (info.duration - start if args.dur is None
                    else min(float(args.dur), info.duration - start))
        key, est = _guard(span_sec, chunk_sec, model)

        print(f"素材      : {paths.name} / {paths.video.name}")
        print(f"長さ      : {info.duration:.0f} 秒（{info.duration / 60:.1f} 分）")
        if span:
            print(f"流す範囲  : {start:.0f}〜{start + span_sec:.0f} 秒"
                  f"（{span_sec:.0f} 秒）")
        print(f"モデル    : {model}")
        print(f"チャンク  : {chunk_sec} 秒 × {est['chunks']} 本")
        print(f"想定費用  : ${est['cost']:.4f}")
        print(f"生の記録  : {raw_file}")
        print("edit.json / settings.json / 原本の .srt には書きません。")
        if not args.yes:
            print("\n流すなら --yes を付けて実行してください（課金されます）。")
            return 0

        print("\n文字起こしを流します…")
        t0 = time.time()

        def on_progress(p: dict) -> None:
            msg = p.get("message")
            if msg:
                print(f"  {msg}", flush=True)

        try:
            vocab = project.load_vocab(paths)
            res = gemini.transcribe(
                paths.video, key=key, model=model, chunk_sec=chunk_sec,
                start_sec=start, dur_sec=args.dur,
                terms=vocab["terms"], note=vocab["note"],
                speakers=[s["name"] for s in project.load_settings(paths)
                          if s["name"] and s["name"] != "不明"] or None,
                peaks_out=None,          # 波形は既にある。作り直さない
                diag_out=raw_file,
                progress=on_progress)
        except gemini.GeminiError as e:
            print(f"失敗しました: {e}", file=sys.stderr)
            return 1

        usage.record({
            "set": paths.name,
            "video": paths.video.name,
            "engine": "gemini",
            "mode": "diag",                     # 診断の実行だと分かるようにする
            "model": res["model"],
            "durationSec": res["duration"],
            "chunks": res["chunks"],
            "chunksDone": res["chunksDone"],
            "elapsed": res["elapsed"],
            "status": "done",
            **{k: v for k, v in res["usage"].items() if k != "pricePerMTok"},
        })
        print(f"\n{len(res['segments'])} 件 / ${res['usage']['cost']:.4f}"
              f" / {time.time() - t0:.0f} 秒")

    if not args.hyp and not raw_file.exists():
        print(f"生の記録がありません: {raw_file}\n"
              f"  --no-run を外して流すか、--raw で場所を指定してください",
              file=sys.stderr)
        return 1

    if args.hyp:
        hyp_file = Path(args.hyp)
        segs, doc = diag.load_hyp(hyp_file)
        # .srt にはチャンクの情報が無いので、時刻の集計だけができる形にする
        raw = {**doc, "segments": segs,
               "chunkSec": doc.get("chunkSec") or chunk_sec,
               "chunks": doc.get("chunks") or 1,
               "duration": doc.get("duration") or (segs[-1]["end"] if segs else 0.0),
               "chunkList": doc.get("chunkList") or [],
               "merged": doc.get("merged") or []}
        raw_file = hyp_file
    else:
        raw = json.loads(raw_file.read_text(encoding="utf-8"))

    truth = diag.load_truth(truth_file)
    m = diag.measure(raw, truth)

    out = raw_file.with_name(raw_file.stem.replace(".gemini-raw", "")
                             + ".diag-report.json"
                             if raw_file.suffix == ".srt"
                             else raw_file.name.replace(".gemini-raw", ".diag-report"))
    out.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")

    print()
    print(f"正解      : {truth_file.name}")
    print(f"生の記録  : {raw_file.name}")
    print("-" * 72)
    print(diag.report(m))
    print("-" * 72)
    print(f"報告      : {out}")
    return 0
