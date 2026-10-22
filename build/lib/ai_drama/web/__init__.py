"""Web 界面（只读）。

    ai_drama/web/api.py     store + 磁盘 → 可 json 的字典（纯函数，离线可测）
    ai_drama/web/app.py     HTTP 路由、绑定策略、Range 支持
    ai_drama/web/static/    一个页面，零构建、零依赖（原生 JS，没有打包步骤）

入口：`python run.py web`。
"""

from __future__ import annotations

from .app import STATIC_DIR, Handler, Response, WebApp, make_server, serve

__all__ = ["Handler", "Response", "STATIC_DIR", "WebApp", "make_server", "serve"]
