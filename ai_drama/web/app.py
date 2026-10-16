"""Web 界面的 socket 那一层。**只读。**

为什么先做只读，而不是一个能在浏览器里点"开始生成"的操作台：

  * 花钱的那条路只有一条（CLI 的 `run` / `resume`），它已经带着预算闸门、
    断点续跑、job_id 台账。一个网页按钮要把这些重新想一遍 —— 谁按的、按了几次、
    上一次那个还在跑吗。那是另一个立项，不是一个附带功能。
  * 这里缺的从来不是"能跑"，而是"能看见"：分镜长什么样、这一镜的关键帧是哪张、
    字幕落在第几秒、质检那几条 unchecked 到底是什么。这些东西现在躺在
    `runs/<id>/` 里，只能靠人一个个打开文件。

所以这个服务的定位是**证据浏览器**：它把已经写在磁盘上的东西摆出来，
不产生任何新的东西。写操作一个都没有 —— 不是"暂时没加"，是这一层就不该有。

绑定地址默认 `127.0.0.1`，而且**拒绝**绑到别的网卡上（除非显式给
`--allow-remote`）。它会把未发布的剧本、角色设定、成片都端出去，
而这台机器可能正接着咖啡馆的 Wi-Fi。默认值和 vision / sfx 那两个形态
是一套道理：危险的默认值不能靠"用户应该知道"来兜。
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
import sys
import threading
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from ..core.store import Store, default_db_path
from . import api

#: 静态文件目录（`index.html` / `app.js` / `style.css`）。
STATIC_DIR = Path(__file__).resolve().parent / "static"

_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


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

        if path == "/media":
            rel = (query.get("path") or [""])[0]
            target = api.safe_media_path(self.root, rel, deny=(self.db,))
            if target is None:
                return Response(404, b"not found", "text/plain; charset=utf-8")
            return self._file(target, headers.get("Range", ""))

        return self._static(path)

    def _file(self, target: Path, range_header: str = "") -> Response:
        """读一个文件，支持 Range（`<video>` 拖动进度条要靠它）。

        整份读进内存：这是本机的单人查看器，一次最多也就几百兆，
        换成流式要把错误处理和 Range 边界重写一遍，不值。
        """
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            data = target.read_bytes()
        except OSError:
            return Response(404, b"not found", "text/plain; charset=utf-8")
        total = len(data)
        out = Response(200, data, ctype, {"Accept-Ranges": "bytes"})
        if not range_header.startswith("bytes=") or total == 0:
            return out
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
            return out
        if start > end or start >= total:
            return Response(416, b"", "text/plain; charset=utf-8",
                            {"Content-Range": f"bytes */{total}"})
        end = min(end, total - 1)
        return Response(206, data[start:end + 1], ctype, {
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
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.ctype)
        self.send_header("Content-Length", str(len(resp.body)))
        # 只读的东西不该被缓存住：改完 project.json 刷新看不到变化最让人困惑。
        self.send_header("Cache-Control", "no-store")
        for key, value in resp.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp.body)

    def do_POST(self) -> None:                             # noqa: N802
        """只读服务，没有任何写入口。"""
        self._method_not_allowed()

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
