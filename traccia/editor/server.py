"""ローカル HTTP サーバー。標準ライブラリのみ（依存追加なし）。

動画は Range リクエストで部分配信するので、879MB の mp4 でも
先頭から読まずにシークできる。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__, config, gemini, usage
from . import compare_api, jobs, project, style_api, waveform, worklog, wfp_import
from .jobs import JobError
from .project import ProjectError, SetPaths

LEASES = worklog.Leases()

STATIC_DIR = Path(__file__).parent / "static"
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
CHUNK = 1024 * 512


class Context:
    def __init__(self, resources: Path):
        self.resources = resources
        self._lock = threading.Lock()

    def sets(self) -> list[SetPaths]:
        return project.list_sets(self.resources)

    def get(self, name: str) -> SetPaths:
        s = project.discover_set(self.resources / name)
        if s is None:
            raise ProjectError(f"セット '{name}' が見つかりません")
        return s

    @property
    def lock(self):
        return self._lock


class Handler(BaseHTTPRequestHandler):
    server_version = "SubtitleEditor/1.0"
    protocol_version = "HTTP/1.1"
    ctx: Context  # 起動時に注入

    # ---------- 送信ヘルパ ----------

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str, **extra) -> None:
        self._send_json({"error": message, **extra}, status)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_error_json(404, f"not found: {path.name}")
            return
        ctype, _ = mimetypes.guess_type(path.name)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        # 静的ファイルもキャッシュさせない。直したのにリロードで反映されない、を防ぐ。
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _send_page(self, path: Path) -> None:
        """HTML は配る前に {{VERSION}} を埋める。

        版を出すためだけに JS から 1 往復増やすのが惜しいので、ここで差し込む。
        """
        if not path.is_file():
            self._send_error_json(404, f"not found: {path.name}")
            return
        data = path.read_text(encoding="utf-8").replace("{{VERSION}}", __version__).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _send_media(self, path: Path) -> None:
        """Range 対応のストリーミング配信。"""
        if not path.is_file():
            self._send_error_json(404, "media not found")
            return

        size = path.stat().st_size
        ctype, _ = mimetypes.guess_type(path.name)
        ctype = ctype or "video/mp4"

        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False

        if rng:
            m = RANGE_RE.search(rng)
            if m:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    if e:
                        end = min(int(e), size - 1)
                elif e:  # bytes=-N → 末尾 N バイト
                    start = max(0, size - int(e))
                partial = True

        if start >= size or start > end:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if self.command == "HEAD":
            return

        remaining = length
        try:
            with path.open("rb") as f:
                f.seek(start)
                while remaining > 0:
                    buf = f.read(min(CHUNK, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            # ブラウザがシークで接続を切るのは正常
            pass

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ---------- ルーティング ----------

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/" or path == "/index.html":
                self._send_page(STATIC_DIR / "index.html")
                return
            if path == "/compare":
                self._send_page(STATIC_DIR / "compare.html")
                return
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                target = (STATIC_DIR / rel).resolve()
                if STATIC_DIR.resolve() not in target.parents:
                    self._send_error_json(403, "forbidden")
                    return
                self._send_file(target)
                return
            if path == "/api/sets":
                self._send_json({"sets": [
                    {
                        "name": s.name,
                        "video": s.video.name,
                        "sizeMB": round(s.video.stat().st_size / 1e6),
                        "hasProject": s.project_file.exists(),
                        "hasWaveform": s.waveform_file.exists(),
                        "speakerFiles": sorted(s.speaker_srts.keys()),
                    }
                    for s in self.ctx.sets()
                ]})
                return

            m = re.fullmatch(r"/api/sets/([^/]+)", path)
            if m:
                self._send_json(project.load(self.ctx.get(m.group(1))))
                return

            m = re.fullmatch(r"/api/sets/([^/]+)/waveform", path)
            if m:
                paths = self.ctx.get(m.group(1))
                try:
                    self._send_json(waveform.load(paths.waveform_file))
                except waveform.NotExtracted:
                    self._send_json({"status": "none"}, 200)
                return

            m = re.fullmatch(r"/api/sets/([^/]+)/settings", path)
            if m:
                paths = self.ctx.get(m.group(1))
                self._send_json({"speakers": project.load_settings(paths),
                                 **project.load_vocab(paths)})
                return

            # 残っている版（文字起こしの生出力 / 手作業の確定版 / wfp の最終版）
            m = re.fullmatch(r"/api/sets/([^/]+)/generations", path)
            if m:
                paths = self.ctx.get(m.group(1))
                self._send_json(project.generations(paths))
                return

            # 2 つの版を突き合わせる。left / right は auto / manual / wfp / current
            m = re.fullmatch(r"/api/sets/([^/]+)/compare", path)
            if m:
                paths = self.ctx.get(m.group(1))
                q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                try:
                    self._send_json(compare_api.run(
                        paths,
                        (q.get("left") or ["current"])[0],
                        (q.get("right") or ["manual"])[0]))
                except compare_api.CompareError as e:
                    self._send_error_json(400, str(e))
                return

            # このセットの作法と、そこから作った提案
            m = re.fullmatch(r"/api/sets/([^/]+)/style", path)
            if m:
                paths = self.ctx.get(m.group(1))
                self._send_json(style_api.overview(paths, self.ctx.resources))
                return

            # セットの中にある .wfp（更新日時の新しい順）
            m = re.fullmatch(r"/api/sets/([^/]+)/wfp", path)
            if m:
                paths = self.ctx.get(m.group(1))
                self._send_json({"files": wfp_import.find_wfps(paths)})
                return

            # 取り込む前に見せる中身。ここでは何も書かない
            m = re.fullmatch(r"/api/sets/([^/]+)/wfp/preview", path)
            if m:
                paths = self.ctx.get(m.group(1))
                q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                name = (q.get("file") or [""])[0]
                if not name:
                    self._send_error_json(400, "どの .wfp か指定されていません")
                    return
                try:
                    self._send_json(wfp_import.preview(paths, name))
                except wfp_import.ImportError_ as e:
                    self._send_error_json(400, str(e))
                return

            # 文字起こしの想定費用と、このセットで最後に動かした job。
            # 使うものは query で選べる（gemini=0 / local=small,medium）
            m = re.fullmatch(r"/api/sets/([^/]+)/transcribe", path)
            if m:
                paths = self.ctx.get(m.group(1))
                job = jobs.RUNNER.latest_for(paths.name)
                # keep_blank_values … local= を「ローカルは使わない」と読むため。
                # 既定では空の値が落ちて、選択肢すべてに戻ってしまう。
                q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                opts = {}
                if "gemini" in q:
                    opts["gemini"] = q["gemini"][0] not in ("0", "false", "")
                if "local" in q:
                    opts["localModels"] = [x for x in q["local"][0].split(",") if x]
                else:
                    opts["localModels"] = [c["id"] for c in jobs.local_asr.MODEL_CHOICES]
                self._send_json({"estimate": jobs.estimate_for(paths),
                                 "plan": jobs.plan(paths, opts),
                                 "job": job.to_dict() if job else None,
                                 "busy": bool(jobs.RUNNER.current())})
                return

            if path == "/api/config":
                self._send_json({**config.public(),
                                 "models": gemini.MODEL_CHOICES,
                                 "chunkRange": [config.MIN_CHUNK_SEC, config.MAX_CHUNK_SEC]})
                return

            if path == "/api/usage":
                self._send_json(usage.summary())
                return

            # 作業時間。一覧が見積もりの目安になる
            if path == "/api/worklog":
                self._send_json(worklog.summary(self.ctx.resources))
                return

            m = re.fullmatch(r"/api/sets/([^/]+)/worklog", path)
            if m:
                self._send_json(worklog.load(self.ctx.get(m.group(1))))
                return

            m = re.fullmatch(r"/api/jobs/([^/]+)", path)
            if m:
                job = jobs.RUNNER.get(m.group(1))
                if not job:
                    self._send_error_json(404, "その処理は見つかりません")
                    return
                self._send_json(job.to_dict())
                return

            m = re.fullmatch(r"/media/([^/]+)", path)
            if m:
                self._send_media(self.ctx.get(m.group(1)).video)
                return

            self._send_error_json(404, f"no route: {path}")
        except ProjectError as e:
            self._send_error_json(404, str(e))
        except JobError as e:
            self._send_error_json(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._send_error_json(500, f"{type(e).__name__}: {e}")

    def do_PUT(self):  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/config":
                config.save(self._read_body())
                self._send_json({**config.public(),
                                 "models": gemini.MODEL_CHOICES,
                                 "chunkRange": [config.MIN_CHUNK_SEC, config.MAX_CHUNK_SEC]})
                return

            m = re.fullmatch(r"/api/sets/([^/]+)/settings", path)
            if m:
                body = self._read_body()
                with self.ctx.lock:
                    self._send_json(project.save_settings(self.ctx.get(m.group(1)), body))
                return

            m = re.fullmatch(r"/api/sets/([^/]+)", path)
            if not m:
                self._send_error_json(404, f"no route: {path}")
                return
            body = self._read_body()
            with self.ctx.lock:
                self._send_json(project.save(self.ctx.get(m.group(1)), body))
        except ProjectError as e:
            self._send_error_json(404, str(e))
        except Exception as e:  # noqa: BLE001
            self._send_error_json(500, f"{type(e).__name__}: {e}")

    def do_POST(self):  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            # キーが通るかの確認。生成しないので費用はかからない
            if path == "/api/config/test":
                body = self._read_body()
                key = str(body.get("apiKey") or "").strip()
                if not key or "…" in key:
                    key, _src = config.resolved_key()
                self._send_json(gemini.check_key(key))
                return

            # 作業時間の加算。累積ではなく差分を受ける（取りこぼしても二重にならない）
            m = re.fullmatch(r"/api/sets/([^/]+)/worklog", path)
            if m:
                paths = self.ctx.get(m.group(1))
                body = self._read_body()
                session = str(body.get("sessionId") or "")
                if not LEASES.claim(paths.name, session):
                    # 同じセットを別のタブが計測中。二重に足さない
                    self._send_json({"counted": False,
                                     "reason": "別のタブでこのセットを計測しています",
                                     **worklog.load(paths)})
                    return
                with self.ctx.lock:
                    doc = worklog.add(paths, session_id=session,
                                      add_sec=body.get("addSec") or 0,
                                      snapshot=body.get("snapshot"))
                self._send_json({"counted": True, **doc})
                return

            m = re.fullmatch(r"/api/sets/([^/]+)/transcribe", path)
            if m:
                paths = self.ctx.get(m.group(1))
                job = jobs.start_transcribe(paths, self._read_body(), self.ctx.lock)
                self._send_json(job.to_dict())
                return

            # wfp を取り込む。手作業版は直前に退避される（TRAC-21）
            m = re.fullmatch(r"/api/sets/([^/]+)/wfp/import", path)
            if m:
                paths = self.ctx.get(m.group(1))
                body = self._read_body()
                name = str(body.get("file") or "")
                mapping = body.get("mapping") or {}
                if not name:
                    self._send_error_json(400, "どの .wfp か指定されていません")
                    return
                try:
                    with self.ctx.lock:
                        res = wfp_import.run_import(paths, name, mapping)
                except wfp_import.ImportError_ as e:
                    self._send_error_json(400, str(e))
                    return
                self._send_json({**res, **project.generations(paths)})
                return

            # 提案を反映する / 取り消す
            m = re.fullmatch(r"/api/sets/([^/]+)/style/(apply|undo)", path)
            if m:
                paths = self.ctx.get(m.group(1))
                body = self._read_body() if m.group(2) == "apply" else {}
                with self.ctx.lock:
                    res = style_api.act(paths, self.ctx.resources,
                                        m.group(2), body)
                self._send_json(res)
                return

            # いまの状態を手作業の確定版として置き直す。やり直しのための出口
            m = re.fullmatch(r"/api/sets/([^/]+)/generations/manual", path)
            if m:
                paths = self.ctx.get(m.group(1))
                if not paths.project_file.exists():
                    self._send_error_json(400, "編集データがありません")
                    return
                with self.ctx.lock:
                    res = project.mark_manual(paths)
                self._send_json({**res, **project.generations(paths)})
                return

            m = re.fullmatch(r"/api/jobs/([^/]+)/cancel", path)
            if m:
                job = jobs.RUNNER.get(m.group(1))
                if not job:
                    self._send_error_json(404, "その処理は見つかりません")
                    return
                job.cancel()
                self._send_json(job.to_dict())
                return

            m = re.fullmatch(r"/api/sets/([^/]+)/export", path)
            if m:
                body = self._read_body()
                with self.ctx.lock:
                    paths = self.ctx.get(m.group(1))
                    project.save(paths, body)          # 書き出す前に必ず保存
                    self._send_json(project.export(paths, body))
                return

            m = re.fullmatch(r"/api/sets/([^/]+)/waveform", path)
            if m:
                paths = self.ctx.get(m.group(1))
                try:
                    data = waveform.extract(paths.video, paths.waveform_file)
                    self._send_json(data)
                except waveform.ExtractionUnavailable as e:
                    self._send_json({"status": "unavailable", "message": str(e)}, 501)
                return

            self._send_error_json(404, f"no route: {path}")
        except ProjectError as e:
            self._send_error_json(404, str(e))
        except JobError as e:
            # 使わない設定・キー未設定・二重起動・上限超え。押した人に見せる
            self._send_error_json(409, str(e))
        except gemini.GeminiError as e:
            self._send_error_json(502, str(e))
        except Exception as e:  # noqa: BLE001
            self._send_error_json(500, f"{type(e).__name__}: {e}")

    def log_message(self, fmt, *args):  # 動画チャンクでログが溢れるので黙らせる
        if os.environ.get("SUBTITLE_EDITOR_VERBOSE"):
            super().log_message(fmt, *args)


class QuietServer(ThreadingHTTPServer):
    """接続が切られただけの例外でトレースバックを出さないサーバー。

    ブラウザは動画のシークのたびに読みかけの接続を捨てる。既定の
    ThreadingHTTPServer はそれを毎回スタックトレース付きで出すので、
    起動しっぱなしのターミナルが数十行の例外で埋まってしまう。
    """

    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: D102
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def _free_port(preferred: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def serve(resources: Path, port: int = 8791, open_browser: bool = True) -> None:
    requested = port
    port = _free_port(port)
    Handler.ctx = Context(resources)
    httpd = QuietServer(("127.0.0.1", port), Handler)

    url = f"http://127.0.0.1:{port}/"
    sets = project.list_sets(resources)
    # ランチャーから起動するとパイプ越しになりバッファに溜まるので、必ず流す
    print(f"Traccia  {url}", flush=True)
    print(f"  素材フォルダ : {resources}", flush=True)
    print(f"  セット       : {', '.join(s.name for s in sets) if sets else '（見つかりません）'}", flush=True)
    if port != requested:
        print(f"  ※ ポート {requested} は使用中だったので {port} を使います", flush=True)
    print("  終了         : Ctrl+C", flush=True)

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました。", flush=True)
    finally:
        httpd.server_close()
