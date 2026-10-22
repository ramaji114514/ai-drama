"""Web 界面的 socket 那一层。

它**不建新 run**：花钱的那条路只有一条（CLI 的 `run` / `resume`），它已经
带着预算闸门、断点续跑、job_id 台账。一个"开始生成"的网页按钮要把这些
重新想一遍 —— 谁按的、按了几次、上一次那个还在跑吗。

写入口只有两个，都发生在**已经有产物**的 run 上：

  * `POST /api/runs/<id>/shots/<label>/prompt`：改一镜的提示词（写
    `episode.json` + 回灌库，见 `core/editing.py`）；
  * `POST /api/runs/<id>/rerun`：只重跑这几镜，后台线程执行，返回一个
    job id 让页面轮询 —— 逐镜重跑不该让浏览器等在那儿。

其余仍然是**证据浏览器**：把已经写在磁盘上的东西摆出来。改动作、改台词、
改出镜人这些会牵连结构的字段不在这里动，仍然走 `resume --from-json`。

绑定地址默认 `127.0.0.1`，而且**拒绝**绑到别的网卡上（除非显式给
`--allow-remote`）。它会把未发布的剧本、角色设定、成片都端出去，
而这台机器可能正接着咖啡馆的 Wi-Fi。默认值和 vision / sfx 那两个形态
是一套道理：危险的默认值不能靠"用户应该知道"来兜。
"""

from __future__ import annotations

import json
import mimetypes
import uuid
import posixpath
import sys
import threading
import traceback
from datetime import datetime
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from ..core import editing
from ..core.store import Store, default_db_path
from . import api

#: 静态文件目录（`index.html` / `app.js` / `style.css`）。
STATIC_DIR = Path(__file__).resolve().parent / "static"

_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")

#: 后台重跑作业：job_id -> {state, ...}。进程内内存，重启就没了 ——
#: 这是本机单人工具，谁也不会隔天回来查一个后台任务。
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def job_status(job_id: str) -> Optional[Dict[str, Any]]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def start_job(root: Path, db: Path, run_id: str, labels: List[str],
              stages: List[str]) -> Dict[str, Any]:
    """开一个后台线程重跑这几镜，立刻返回作业卡。"""
    with _JOBS_LOCK:
        # 只允许一个重跑在跑：两个线程同时写同一份 episode.json 和同一个分镜
        # 目录，出来的东西谁也说不清是谁的。
        busy = [j for j in _JOBS.values() if j.get("state") == "running"]
        if busy:
            one = busy[0]
            raise editing.EditError(
                f"已经有一个重跑在跑（{one['run_id']} 的 {'/'.join(one['labels'])}）。"
                f"等它结束再点。", code=409)
        job_id = uuid.uuid4().hex[:12]
        job: Dict[str, Any] = {
            "id": job_id, "run_id": run_id, "labels": list(labels),
            "stages": list(stages), "state": "running",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": "", "error": "", "result": {},
        }
        _JOBS[job_id] = job
    threading.Thread(target=_run_job, args=(job_id, root, db, run_id, labels, stages),
                     daemon=True).start()
    return dict(job)


def _run_job(job_id: str, root: Path, db: Path, run_id: str,
             labels: List[str], stages: List[str]) -> None:
    store = Store(db)          # 线程里自己开一个：sqlite 连接不能跨线程共用
    try:
        result = editing.rerun_shots(root, run_id, labels, store=store, stages=stages)
        state, error = "done", ""
    except Exception as exc:   # noqa: BLE001  后台线程没人接异常
        traceback.print_exc()
        result, state, error = {}, "failed", f"{type(exc).__name__}: {exc}"
    finally:
        store.close()
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job["state"] = state
            job["error"] = error
            job["result"] = {"state": (result or {}).get("state", ""),
                             "failed_stage": (result or {}).get("failed_stage") or ""}
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    ctype: str = "application/octet-stream"
    headers: Dict[str, str] = field(default_factory=dict)


class WebApp:
    """一次请求要用到的那点上下文。

    刻意不是模块级全局：测试要能同时开两个指向不同工作目录的服务，
    而"这个进程只有一个工作目录"正是那种到生产上才会发现的假设。
    """

    def __init__(self, root: Path, db: Path, *, limit: int = 50, quiet: bool = False):
        self.root = Path(root).resolve()
        self.db = Path(db).resolve()
        self.limit = int(limit)
        self.quiet = quiet

    # 每个请求开一个 Store：sqlite3 连接不是线程安全的，而这个 handler 跑在
    # ThreadingHTTPServer 上。开连接很便宜，比在这里维护连接池不容易出错。
    def store(self) -> Store:
        return Store(self.db)

    def _json(self, obj: Any, status: int = 200) -> Response:
        return Response(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def handle(self, path: str, query: Dict[str, List[str]],
               headers: Optional[Dict[str, str]] = None) -> Response:
        """把一个 URL 变成响应。

        路由写在这里而不是散在 `do_GET` 里，是为了让它能被直接单测 ——
        不用起端口就能验"这个路径该不该给"。
        """
        headers = headers or {}

        if path == "/api/meta":
            return self._json(api.meta_payload(self.root, self.db))

        if path == "/api/runs":
            store = self.store()
            try:
                return self._json(api.runs_payload(self.root, store, limit=self.limit))
            finally:
                store.close()

        if path.startswith("/api/runs/"):
            tail = unquote(path[len("/api/runs/"):]).strip("/")
            store = self.store()
            try:
                if tail.endswith("/assets"):
                    run_id = tail[: -len("/assets")].strip("/")
                    if not run_id:
                        return self._json({"error": "缺少 run_id"}, 404)
                    return self._json(api.assets_payload(self.root, store, run_id))
                payload = api.run_payload(self.root, store, tail)
            finally:
                store.close()
            if payload is None:
                return self._json({"error": f"没有这条 run：{tail}"}, 404)
            return self._json(payload)

        if path.startswith("/api/assets/"):
            asset_id = unquote(path[len("/api/assets/"):]).strip("/")
            store = self.store()
            try:
                payload = api.asset_payload(self.root, store, asset_id)
            finally:
                store.close()
            if payload is None:
                return self._json({"error": f"台账里没有这个 asset_id：{asset_id}"}, 404)
            return self._json(payload)

        if path == "/api/projects":
            return self._json(api.projects_payload(self.root))

        if path.startswith("/api/projects/"):
            project_id = unquote(path[len("/api/projects/"):]).strip("/")
            payload = api.project_payload(self.root, project_id)
            if payload is None:
                return self._json({"error": f"没有这个项目：{project_id}"}, 404)
            return self._json(payload)

        if path.startswith("/api/jobs/"):
            job_id = unquote(path[len("/api/jobs/"):]).strip("/")
            job = job_status(job_id)
            if job is None:
                return self._json({"error": f"没有这个作业：{job_id}"}, 404)
            return self._json(job)

        if path == "/media":
            rel = (query.get("path") or [""])[0]
            target = api.safe_media_path(self.root, rel, deny=(self.db,))
            if target is None:
                return Response(404, b"not found", "text/plain; charset=utf-8")
            return self._file(target, headers.get("Range", ""))

        return self._static(path)

    # ————————————————————————————————————————----
    # 两个写入口
    # ————————————————————————————————————————----

    def handle_post(self, path: str, body: bytes) -> Response:
        if path.startswith("/api/jobs/"):
            return self._json({"error": "作业只读：直接 GET /api/jobs/<id>"}, 405)
        if path.startswith("/api/runs/"):
            tail = unquote(path[len("/api/runs/"):]).strip("/")
            # 镜头标签本身带斜杠（sc01/shot_000），所以不能按段数切。
            if tail.endswith("/prompt") and "/shots/" in tail:
                run_id, _, label = tail[: -len("/prompt")].partition("/shots/")
                if run_id and label:
                    return self._edit_prompt(run_id, label, body)
            if tail.endswith("/story") and "/shots/" in tail:
                run_id, _, label = tail[: -len("/story")].partition("/shots/")
                if run_id and label:
                    return self._edit_story(run_id, label, body)
            if tail.endswith("/rerun"):
                run_id = tail[: -len("/rerun")].strip("/")
                if run_id and "/" not in run_id:
                    return self._start_rerun(run_id, body)
        return self._json({"error": f"没有这个接口：POST {path}"}, 404)

    def _body_json(self, body: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise editing.EditError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise editing.EditError("请求体要是一个对象")
        return data

    def _edit_story(self, run_id: str, label: str, body: bytes) -> Response:
        """改动作/台词/结束画面/景别，然后用本地骨架重拼提示词。"""
        from ..providers import registry
        from ..providers.base import caps_of

        store = self.store()
        try:
            data = self._body_json(body)
            fields = {k: str(v) for k, v in data.items()
                      if k in editing.STORY_FIELDS and v is not None}
            if not fields:
                raise editing.EditError(
                    f"没有要改的字段（可改：{'、'.join(editing.STORY_FIELDS)}）")
            # 参考图名额要问 provider：换一家支持多张参考图的模型，
            # 这里重拼出来的提示词就该跟着变，不能写死 1。
            max_refs = int(getattr(caps_of(registry.build_bundle().image),
                                   "max_ref_images", 1) or 1)
            shot = editing.update_story(self.root, run_id, label, fields,
                                        store=store, max_refs=max_refs)
            return self._json({"ok": True, "shot": shot})
        except editing.EditError as exc:
            return self._json({"error": exc.message}, exc.code)
        finally:
            store.close()

    def _edit_prompt(self, run_id: str, label: str, body: bytes) -> Response:
        store = self.store()
        try:
            data = self._body_json(body)
            field = str(data.get("field") or "")
            text = data.get("text")
            if not isinstance(text, str):
                raise editing.EditError("text 要是字符串（想清空就给空串）")
            shot = editing.update_prompt(self.root, run_id, label, field, text,
                                         store=store)
            return self._json({"ok": True, "shot": shot})
        except editing.EditError as exc:
            return self._json({"error": exc.message}, exc.code)
        finally:
            store.close()

    def _start_rerun(self, run_id: str, body: bytes) -> Response:
        try:
            data = self._body_json(body)
            raw = data.get("labels") or data.get("shots") or []
            if isinstance(raw, str):
                raw = [raw]
            labels = [str(x).strip() for x in raw if str(x).strip()]
            if not labels:
                raise editing.EditError("至少给一镜（labels）")
            stages = [str(s) for s in (data.get("stages") or ["frames"])]
        except editing.EditError as exc:
            return self._json({"error": exc.message}, exc.code)

        try:
            job = start_job(self.root, self.db, run_id, labels, stages)
        except editing.EditError as exc:
            return self._json({"error": exc.message}, exc.code)
        return self._json({"ok": True, "job": job}, 202)

    def _file(self, target: Path, range_header: str = "") -> Response:
        """读一个文件，支持 Range（`<video>` 拖动进度条要靠它）。

        **只读请求的那一段**，不再整份进内存：一集成片几百兆，而 `<video>`
        拖一下进度条就会发一个 Range 请求 —— 每拖一次读一遍整个文件，
        内存和磁盘都白挨一遍。做法是 seek 到起点再 read 需要的长度。
        """
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            total = target.stat().st_size
        except OSError:
            return Response(404, b"not found", "text/plain; charset=utf-8")
        if total == 0:
            return Response(200, b"", ctype, {"Accept-Ranges": "bytes"})

        start, end = 0, total - 1
        partial = False
        if range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].split(",")[0].strip()
            first, _, last = spec.partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else total - 1
                else:
                    start = max(0, total - int(last))
                    end = total - 1
            except ValueError:
                start, end = 0, total - 1        # 头写坏了：整份给，别 500
            if start > end or start >= total:
                return Response(416, b"", "text/plain; charset=utf-8",
                                {"Content-Range": f"bytes */{total}"})
            end = min(end, total - 1)
            partial = True

        try:
            with target.open("rb") as fh:
                fh.seek(start)
                data = fh.read(end - start + 1)
        except OSError:
            return Response(404, b"not found", "text/plain; charset=utf-8")

        if not partial:
            return Response(200, data, ctype, {"Accept-Ranges": "bytes"})
        return Response(206, data, ctype, {
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{total}",
        })

    def _static(self, path: str) -> Response:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR != target and STATIC_DIR not in target.parents:
            return Response(404, b"not found", "text/plain; charset=utf-8")
        if not target.is_file():
            # 单页界面：未知路径交回 index.html，让前端自己显示 404。
            if "." in posixpath.basename(rel):
                return Response(404, b"not found", "text/plain; charset=utf-8")
            target = STATIC_DIR / "index.html"
            if not target.is_file():
                return Response(404, b"not found", "text/plain; charset=utf-8")
        return self._file(target)


class Handler(BaseHTTPRequestHandler):
    server_version = f"ai-drama/{__version__}"
    app: WebApp                                  # 由 make_server 造的子类注入

    def log_message(self, fmt: str, *a: Any) -> None:      # noqa: A003
        if not getattr(self.app, "quiet", False):
            sys.stderr.write("  %s\n" % (fmt % a))

    def do_GET(self) -> None:                              # noqa: N802
        self._respond()

    def do_HEAD(self) -> None:                             # noqa: N802
        self._respond()

    def _respond(self) -> None:
        parsed = urlparse(self.path)
        try:
            resp = self.app.handle(
                unquote(parsed.path), parse_qs(parsed.query),
                {k.title(): v for k, v in self.headers.items()},
            )
        except Exception as exc:                           # noqa: BLE001
            # 故意把 traceback 打全：这是本机调试工具，藏起来只会让人去猜。
            traceback.print_exc()
            resp = Response(500, json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
            ).encode("utf-8"), "application/json; charset=utf-8")
        self._send(resp)

    def _send(self, resp: Response) -> None:
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.ctype)
        self.send_header("Content-Length", str(len(resp.body)))
        # 不该被缓存住：改完提示词刷新看不到变化最让人困惑。
        self.send_header("Cache-Control", "no-store")
        for key, value in resp.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp.body)

    def do_POST(self) -> None:                             # noqa: N802
        parsed = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        # 上限 1MB：一条提示词再长也就几万字符。超过的不读，免得被一个
        # 匿名请求决定我们花多少内存。
        body = self.rfile.read(n) if 0 < n <= (1 << 20) else b""
        try:
            resp = self.app.handle_post(unquote(parsed.path), body)
        except Exception as exc:                           # noqa: BLE001
            traceback.print_exc()
            resp = Response(500, json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
            ).encode("utf-8"), "application/json; charset=utf-8")
        self._send(resp)

    def do_PUT(self) -> None:                              # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:                           # noqa: N802
        self._method_not_allowed()

    def _drain_request_body(self, limit: int = 1 << 20) -> None:
        """把请求体读掉，再回 405。

        不读就走，socket 里留着没被消费的数据，连接关闭时 Windows 会发 RST ——
        客户端收到的不是那个礼貌的 405，而是 `ConnectionAbortedError`。
        这条是被一次偶发失败的测试逼出来的：那个用例单跑必过、全量偶挂。

        只读服务没有"解析这个 body"的需求，所以读完就扔。超过 `limit` 的不读：
        让一个匿名请求决定我们花多少内存，是把只读做成了可被喂胖。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < n <= limit:
            self.rfile.read(n)

    def _method_not_allowed(self) -> None:
        self._drain_request_body()
        body = json.dumps(
            {"error": "这个服务是只读的。要跑片子请用 CLI 的 run / resume —— "
                      "花钱的那条路只留一条，才能把预算闸门和 job_id 台账都守住。"},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(405)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Allow", "GET, HEAD")
        self.end_headers()
        self.wfile.write(body)


def _handler_for(app: WebApp) -> type:
    """给每个服务造一个把 `app` 挂在类上的 Handler。

    不能用 `partial(Handler)` 顺手塞一个属性：`partial` 上的属性不会变成
    实例属性，`self.app` 会直接 AttributeError（这个坑我踩过）。
    """

    class _Handler(Handler):
        pass

    _Handler.app = app
    return _Handler


def make_server(root: Path, db: Optional[Path] = None, *, host: str = "127.0.0.1",
                port: int = 8765, limit: int = 50, quiet: bool = False,
                allow_remote: bool = False) -> Tuple[ThreadingHTTPServer, WebApp]:
    """建一个（还没开始 serve 的）服务器。测试用 `port=0` 拿随机端口。"""
    if host not in _LOOPBACK and not allow_remote:
        raise ValueError(
            f"拒绝绑定到 {host}：这个服务会把未发布的剧本、角色设定和成片"
            f"端到那个地址上。真要让局域网里的别人看，加上 --allow-remote。"
        )
    dbfile = Path(db) if db else default_db_path()
    app = WebApp(root, dbfile, limit=limit, quiet=quiet)
    httpd = ThreadingHTTPServer((host, port), _handler_for(app))
    return httpd, app


def serve(root: Path, db: Optional[Path] = None, *, host: str = "127.0.0.1",
          port: int = 8765, limit: int = 50, quiet: bool = False,
          allow_remote: bool = False, open_browser: bool = False) -> int:
    httpd, app = make_server(root, db, host=host, port=port, limit=limit,
                             quiet=quiet, allow_remote=allow_remote)
    actual = httpd.server_address[1]
    shown = "127.0.0.1" if host in _LOOPBACK else host
    url = f"http://{shown}:{actual}/"
    print(f"ai-drama {__version__}  Web 界面（只读）")
    print(f"  工作目录  {app.root}")
    print(f"  数据库    {app.db}")
    print(f"  地址      {url}")
    if host not in _LOOPBACK:
        print("  [!] 绑在非回环地址上：同一网络里的人都能看到这些片子。")
    print("  Ctrl-C 停止。")
    if open_browser:
        import webbrowser

        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


__all__ = ["Handler", "Response", "STATIC_DIR", "WebApp", "make_server", "serve"]
