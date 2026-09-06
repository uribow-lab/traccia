"""時間のかかる処理をバックグラウンドで回す。いまのところ文字起こしだけ。

1 時間の動画で 10 分前後かかるので、HTTP の 1 往復では返せない。
開始だけ受け付けて job を返し、画面は進捗を取りに来る。

同時に走らせるのは 1 本まで。二重に押してしまったときに、気づかないまま
2 回ぶん課金される事故を防ぐ。
"""

from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

from .. import config, gemini, local_asr, merge as merge_mod, style as style_mod, usage
from . import project
from .project import SetPaths


class JobError(Exception):
    """開始できない理由。文面はそのまま画面に出る。"""


class Job:
    def __init__(self, job_id: str, set_name: str, kind: str, estimate: dict):
        self.id = job_id
        self.set_name = set_name
        self.kind = kind
        self.status = "running"        # running / done / cancelled / error
        self.message = "準備しています…"
        self.phase = ""
        self.chunk = 0
        self.chunks = int(estimate.get("chunks") or 0)
        self.cost = 0.0                # ここまでの実測（概算額）
        self.estimate = estimate
        self.error = ""
        self.result: dict | None = None
        self.imported = 0
        # 系統ごとの進み具合。{"gemini": {...}, "small": {...}, "medium": {...}}
        self.engines: dict[str, dict] = {}
        self.merged: dict | None = None
        self.notes: list[str] = []       # 警告（暴走・はみ出しなど）
        self.started = time.time()
        self.finished: float | None = None
        self._cancel = threading.Event()

    # ---- 状態 ----

    def cancel(self) -> None:
        self._cancel.set()
        self.message = "中止しています…（いま送っている 1 本が終わるまで待ちます）"

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "set": self.set_name,
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "phase": self.phase,
            "chunk": self.chunk,
            "chunks": self.chunks,
            "cost": round(self.cost, 5),
            "estimate": self.estimate,
            "error": self.error,
            "imported": self.imported,
            "engines": self.engines,
            "merged": self.merged,
            "notes": self.notes,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "startedAt": self.started,
            "finishedAt": self.finished,
            "result": self.result,
        }


class Runner:
    """走っている job と、直前に終わった job を覚えておく。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._current: Job | None = None
        self._seq = 0

    def current(self) -> Job | None:
        with self._lock:
            return self._current

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest_for(self, set_name: str) -> Job | None:
        with self._lock:
            cands = [j for j in self._jobs.values() if j.set_name == set_name]
        return max(cands, key=lambda j: j.started) if cands else None

    def start(self, set_name: str, kind: str, estimate: dict, work) -> Job:
        with self._lock:
            if self._current and self._current.status == "running":
                raise JobError(
                    f"「{self._current.set_name}」の処理がまだ動いています。"
                    "終わるか中止するまで待ってください")
            self._seq += 1
            job = Job(f"job{self._seq}", set_name, kind, estimate)
            self._jobs[job.id] = job
            self._current = job
            # 古い記録は溜めない。直近 20 件だけ残す
            if len(self._jobs) > 20:
                for old in sorted(self._jobs.values(), key=lambda j: j.started)[:-20]:
                    self._jobs.pop(old.id, None)

        t = threading.Thread(target=self._run, args=(job, work), daemon=True)
        t.start()
        return job

    def _run(self, job: Job, work) -> None:
        try:
            work(job)
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            job.message = "失敗しました"
            traceback.print_exc()
        finally:
            job.finished = time.time()
            with self._lock:
                if self._current is job:
                    self._current = None


RUNNER = Runner()


def _set_engine(job: Job, name: str, **kw) -> None:
    """系統ごとの進み具合を更新する。画面はこれを 1 行ずつ出す。"""
    cur = job.engines.setdefault(name, {"name": name, "status": "waiting",
                                        "at": 0.0, "duration": 0.0, "message": ""})
    cur.update(kw)


def _eta(cur: dict) -> float | None:
    """残り時間の目安。無音は速く会話が詰まった所は遅いので、平均で均す。"""
    at, dur, started = cur.get("at") or 0, cur.get("duration") or 0, cur.get("startedAt")
    if not (at > 0 and dur > 0 and started):
        return None
    spent = time.time() - started
    return max(0.0, spent / at * (dur - at))


# ---------------------------------------------------------------- 文字起こし

def _guard(duration: float) -> tuple[str, dict, dict]:
    """流してよいかを確かめて、キーと設定と見積もりを返す。

    画面のボタンを隠すだけでは保険にならないので、サーバー側でも同じ判断をする。
    """
    cfg = config.load()["gemini"]
    if not cfg["enabled"]:
        raise JobError("Gemini 文字起こしが「使わない」になっています。"
                       "設定で「使う」に切り替えてください")
    key, _src = config.resolved_key()
    if not key:
        raise JobError("Gemini の API キーが設定されていません。設定で入れてください")

    est = gemini.estimate(duration, cfg["model"], cfg["chunkSec"])

    limit = float(cfg.get("monthlyLimitUsd") or 0.0)
    if limit > 0:
        spent = usage.month_cost()
        if spent + est["cost"] > limit:
            raise JobError(
                f"今月の上限 ${limit:.2f} を超えます"
                f"（今月ここまで ${spent:.2f} ＋ 今回の想定 ${est['cost']:.2f}）。"
                "設定で上限を上げるか、来月まで待ってください")
    return key, cfg, est


def estimate_for(paths: SetPaths) -> dict:
    """実行前に画面へ出す想定費用。設定が不十分でも数字だけは見せる。"""
    cfg = config.load()["gemini"]
    info = project.probe(paths.video)
    duration = info.duration
    if not duration:
        raise JobError("動画の長さを読み取れませんでした（PyAV が必要です）")

    est = gemini.estimate(duration, cfg["model"], cfg["chunkSec"])
    key, _src = config.resolved_key()
    return {
        **est,
        "ready": bool(key) and bool(cfg["enabled"]),
        "hasKey": bool(key),
        "enabled": bool(cfg["enabled"]),
        "monthCost": round(usage.month_cost(), 5),
        "monthlyLimitUsd": cfg["monthlyLimitUsd"],
        "totalCost": usage.summary(limit=0)["totalCost"],
    }


def plan(paths: SetPaths, options: dict) -> dict:
    """選んだ組み合わせでの想定費用と所要時間。実行前の確認画面に出す。

    Gemini は課金されるので額を、ローカルは無料だが時間がかかるので分を出す。
    """
    cfg = config.load()["gemini"]
    info = project.probe(paths.video)
    duration = info.duration
    if not duration:
        raise JobError("動画の長さを読み取れませんでした（PyAV が必要です）")

    use_gemini = options.get("gemini", True)
    models = list(options.get("localModels") or [])

    est = gemini.estimate(duration, cfg["model"], cfg["chunkSec"])
    cost = est["cost"] if use_gemini else 0.0

    # Gemini は通信待ち、ローカルは CPU 待ちなので重ねられる。ローカル同士は
    # 取り合うので足し合わせる（実装時の実測に合わせて見直す）。
    local_sec = sum(local_asr.estimate_sec(duration, m) for m in models)
    gem_sec = duration * 0.19 if use_gemini else 0.0     # 実測 1737 秒 → 338 秒
    return {
        **est,
        "cost": round(cost, 4),
        "useGemini": use_gemini,
        "localModels": models,
        "localAvailable": local_asr.available(),
        "localChoices": local_asr.MODEL_CHOICES,
        "geminiSec": round(gem_sec),
        "localSec": round(local_sec),
        "totalSec": round(max(gem_sec, 0) + local_sec if models else gem_sec),
        "monthCost": round(usage.month_cost(), 5),
        "monthlyLimitUsd": cfg["monthlyLimitUsd"],
    }


def start_transcribe(paths: SetPaths, options: dict, lock: threading.Lock) -> Job:
    """文字起こしを開始する。lock は保存とぶつからないようにするためのもの。

    Gemini（本文と話者）とローカル（時刻）を並行して回し、最後に合成する。
    どれを使うかは実行前の画面で選ぶ。Gemini を外してローカルだけでも作れる。
    """
    info = project.probe(paths.video)
    duration = info.duration
    if not duration:
        raise JobError("動画の長さを読み取れませんでした（PyAV が必要です）")

    use_gemini = bool(options.get("gemini", True))
    # ローカルだけが拾った発話も足すか。既定では足さない（traccia/merge.py の
    # merge() を見よ）。拾える発話より、言い直しや被った声の聞き間違いのほうが
    # 多く混ざるので、必要なときだけ実行前の画面で入れてもらう。
    pick_up = bool(options.get("pickUp", False))
    models = [m for m in (options.get("localModels") or [])
              if m in {c["id"] for c in local_asr.MODEL_CHOICES}]
    if not use_gemini and not models:
        raise JobError("Gemini とローカルのどちらも選ばれていません")
    if models and not local_asr.available():
        raise JobError("ローカル文字起こしが使えません"
                       "（faster-whisper が入っていない）")

    key, cfg, est = ("", config.load()["gemini"], {"chunks": 0, "cost": 0.0})
    if use_gemini:
        key, cfg, est = _guard(duration)

    vocab = project.load_vocab(paths)
    terms = vocab["terms"]
    note = vocab["note"]

    # 話者名は、すでに設定してあるものを優先して使わせる。
    # チャンクをまたいで A と B が入れ替わるのを抑えるため。
    named = [s["name"] for s in project.load_settings(paths)
             if s["name"] and s["name"] != "不明"]
    if options.get("useSpeakerNames") is False:
        named = []
    try:
        num_speakers = int(options.get("speakers") or 0) or None
    except (TypeError, ValueError):
        num_speakers = None

    # このセットの確定版から測った作法を、プロンプトに差し込む（TRAC-24）。
    # 測れなければ同じ場所の他セットの中央値、それも無ければ既定値。
    sty = style_mod.effective(paths.root, paths.stem, paths.root.parent)
    style_lines = style_mod.prompt_lines(sty)

    def work(job: Job) -> None:
        job.phase = "audio"
        job.message = "音声を取り出しています…"
        for name in (["gemini"] if use_gemini else []) + models:
            _set_engine(job, name, status="waiting", duration=duration)

        # 音声のデコードは 1 回だけ。2.2GB の素材で 21 秒かかるので、
        # Gemini とローカルで読み直さない。
        samples = gemini.load_audio(paths.video)
        if not paths.waveform_file.exists():
            job.message = "波形を作っています…"
            gemini.write_peaks(samples, paths.waveform_file)

        results: dict[str, object] = {}
        errors: dict[str, str] = {}

        def run_gemini() -> None:
            _set_engine(job, "gemini", status="running", startedAt=time.time())

            def on_progress(p: dict) -> None:
                if p.get("chunk"):
                    job.chunk = int(p["chunk"])
                if p.get("chunks"):
                    job.chunks = int(p["chunks"])
                if p.get("cost") is not None:
                    job.cost = float(p["cost"])
                # はみ出しの警告は、終わったあとに overruns からまとめて出す。
                # ここでも積むと「8 本目は…」と「Gemini 8 本目は…」が二重に並ぶ。
                msg = p.get("message") or ""
                _set_engine(job, "gemini", message=msg,
                            at=(job.chunk / job.chunks * duration) if job.chunks else 0.0,
                            cost=job.cost)

            try:
                res = gemini.transcribe(
                    paths.video, key=key, model=cfg["model"],
                    chunk_sec=cfg["chunkSec"], terms=terms, note=note,
                    speakers=named or None, num_speakers=num_speakers,
                    peaks_out=None, style=style_lines,
                    progress=on_progress, cancel=job.cancelled)
                results["gemini"] = res
                # 途中経過で入れた額を、確定した額で置き直す。しないと系統の行と
                # 下の「ここまでの実測」が食い違ったまま残る
                job.cost = float(res["usage"]["cost"])
                _set_engine(job, "gemini", status="done", at=duration,
                            cost=job.cost,
                            message=f"{len(res['segments'])} 件 / ${job.cost:.4f}")
            except gemini.CancelledWithUsage as e:
                results["gemini"] = e.result
                _set_engine(job, "gemini", status="cancelled")
            except gemini.Cancelled:
                _set_engine(job, "gemini", status="cancelled")
            except gemini.GeminiError as e:
                errors["gemini"] = str(e)
                _set_engine(job, "gemini", status="error", message=str(e))

        def run_local(model: str) -> None:
            _set_engine(job, model, status="running", startedAt=time.time())

            def on_progress(p: dict) -> None:
                _set_engine(job, model, at=float(p.get("at") or 0.0),
                            duration=float(p.get("duration") or duration),
                            count=int(p.get("count") or 0),
                            message=p.get("message") or "")

            try:
                res = local_asr.transcribe(
                    samples, model=model, terms=terms, note=note,
                    progress=on_progress, cancel=job.cancelled)
                results[model] = res
                if res.warning and res.warning not in job.notes:
                    job.notes.append(f"{model}: {res.warning}")
                _set_engine(job, model, status="done", at=duration,
                            message=f"{len(res.segments)} 件 / {res.elapsed:.0f} 秒")
            except local_asr.Cancelled:
                _set_engine(job, model, status="cancelled")
            except local_asr.LocalError as e:
                errors[model] = str(e)
                _set_engine(job, model, status="error", message=str(e))

        job.phase = "transcribe"
        job.message = "文字起こし中…"
        threads = []
        if use_gemini:
            threads.append(threading.Thread(target=run_gemini, daemon=True))
        # ローカル同士は CPU を取り合うので順番に回す。1 本のスレッドで直列に。
        if models:
            def run_locals() -> None:
                for m in models:
                    if job.cancelled():
                        _set_engine(job, m, status="cancelled")
                        continue
                    run_local(m)
            threads.append(threading.Thread(target=run_locals, daemon=True))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if job.cancelled() and not results:
            job.status = "cancelled"
            job.message = "中止しました"
            return
        if not results:
            job.status = "error"
            job.error = " / ".join(f"{k}: {v}" for k, v in errors.items()) or "結果がありません"
            job.message = "失敗しました"
            return

        # 使った額は、途中で止めても必ず残す
        gem = results.get("gemini")
        if gem:
            job.cost = float(gem["usage"]["cost"])
            usage.record({
                "set": paths.name, "video": paths.video.name, "engine": "gemini",
                "model": gem["model"], "durationSec": gem["duration"],
                "chunks": gem["chunks"], "chunksDone": gem["chunksDone"],
                "elapsed": gem["elapsed"], "status": job.status,
                **{k: v for k, v in gem["usage"].items() if k != "pricePerMTok"},
            })
            for o in gem.get("overruns") or []:
                msg = (f"Gemini {o['chunk']} 本目は時刻が {o['over']:.0f} 秒はみ出しています"
                       "（この範囲は時刻がずれている可能性）")
                if msg not in job.notes:
                    job.notes.append(msg)

        job.phase = "merge"
        job.message = "突き合わせています…"
        sources = []
        if gem:
            sources.append(merge_mod.Source("gemini", "text", gem["segments"]))
        for m in models:
            r = results.get(m)
            if r:
                sources.append(merge_mod.Source(m, "time", r.segments))

        cues, rep = merge_mod.merge(sources, pick_up=pick_up)
        job.merged = rep.to_dict()
        job.notes.append(f"区切りの目安 {sty.sec:.1f} 秒・{sty.chars} 文字を使いました"
                         f"（{sty.from_ or '既定値'}）")
        job.result = {
            "duration": round(duration, 3),
            "engines": {k: (v.to_dict() if hasattr(v, "to_dict")
                            else {kk: vv for kk, vv in v.items() if kk != "segments"})
                        for k, v in results.items()},
            "merge": rep.to_dict(),
        }

        if not cues:
            job.status = "error"
            job.error = "区間が 1 つも返りませんでした"
            job.message = "取り込むものがありませんでした"
            return

        with lock:
            res = project.import_segments(paths, cues)
        job.imported = res["count"]
        job.status = "cancelled" if job.cancelled() else "done"
        job.message = f"{res['count']} 件を取り込みました（{merge_mod.summary_text(rep)}）"

    est = {**est, "useGemini": use_gemini, "localModels": models, "pickUp": pick_up}
    return RUNNER.start(paths.name, "transcribe", est, work)
