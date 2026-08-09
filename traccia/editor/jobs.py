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

from .. import config, gemini, usage
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


def start_transcribe(paths: SetPaths, options: dict, lock: threading.Lock) -> Job:
    """文字起こしを開始する。lock は保存とぶつからないようにするためのもの。"""
    info = project.probe(paths.video)
    duration = info.duration
    if not duration:
        raise JobError("動画の長さを読み取れませんでした（PyAV が必要です）")

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

    def work(job: Job) -> None:
        def on_progress(p: dict) -> None:
            job.phase = p.get("phase") or job.phase
            job.message = p.get("message") or job.message
            if p.get("chunk"):
                job.chunk = int(p["chunk"])
            if p.get("chunks"):
                job.chunks = int(p["chunks"])
            if p.get("cost") is not None:
                job.cost = float(p["cost"])

        partial = None
        try:
            res = gemini.transcribe(
                paths.video, key=key, model=cfg["model"], chunk_sec=cfg["chunkSec"],
                terms=terms, note=note, speakers=named or None,
                num_speakers=num_speakers,
                peaks_out=None if paths.waveform_file.exists() else paths.waveform_file,
                progress=on_progress, cancel=job.cancelled)
        except gemini.CancelledWithUsage as e:
            partial = e.result
            job.status = "cancelled"
        except gemini.Cancelled:
            job.status = "cancelled"
            job.message = "中止しました"
            return
        except gemini.GeminiError as e:
            job.status = "error"
            job.error = str(e)
            job.message = "失敗しました"
            return
        else:
            partial = res
            job.status = "done"

        job.result = {k: v for k, v in partial.items() if k != "segments"}
        job.cost = float(partial["usage"]["cost"])

        # 途中で止めても、そこまでの呼び出しには課金されている。必ず残す。
        usage.record({
            "set": paths.name,
            "video": paths.video.name,
            "engine": "gemini",
            "model": partial["model"],
            "durationSec": partial["duration"],
            "chunks": partial["chunks"],
            "chunksDone": partial["chunksDone"],
            "elapsed": partial["elapsed"],
            "status": job.status,
            **{k: v for k, v in partial["usage"].items() if k != "pricePerMTok"},
        })

        segs = partial["segments"]
        if not segs:
            if job.status == "done":
                job.status = "error"
                job.error = "区間が 1 つも返りませんでした"
            job.message = "取り込むものがありませんでした"
            return

        with lock:
            res = project.import_segments(paths, segs)
        job.imported = res["count"]

        if job.status == "cancelled":
            job.message = (f"{partial['chunksDone']}/{partial['chunks']} 本目までを"
                           f"取り込みました（{res['count']} 件 / ${job.cost:.4f}）")
        else:
            job.message = (f"{res['count']} 件を取り込みました"
                           f"（${job.cost:.4f} / {partial['elapsed']:.0f} 秒）")

    return RUNNER.start(paths.name, "transcribe", est, work)
