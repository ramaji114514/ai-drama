"""Web 界面：只读，而且**不许读出去**。

这一层最要紧的两件事都不是"页面能打开"：

  1. **读不出去。** 它按 URL 上的路径去读磁盘，而这个进程能读到的东西
     （`.env`、API key、数据库、别的仓库）比它该暴露的多得多。所以白名单是
     目录级的（只有 `runs/` 和 `projects/`），`..`、绝对路径、盘符、`_work/`、
     点开头的文件、数据库一律回 404 —— 而"不存在"和"不许看"**回同一个 404**，
     分开就等于告诉外面哪些路径是存在的。
  2. **不许自己编。** 判定（质检结论、钱、时间轴）全部来自磁盘上已经写好的报告，
     `api.py` 只做搬运。文件没了就报"文件没了"，绝不拿一张裂图或一个空值蒙过去 ——
     这条和 `assets --check`、`qc` 的"没查成不算通过"是同一条规矩。

页面本身（`static/`）刻意零构建：没有 npm、没有打包步骤，改完刷新就是新的。
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Tuple

import pytest

from ai_drama.core import paths
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry
from ai_drama.web import api
from ai_drama.web.app import STATIC_DIR, make_server


def _run_pipeline(workspace: Path) -> str:
    store = Store(workspace / "runs" / "test.sqlite3")
    pipe = Pipeline(workspace, store, registry.build_bundle(), concurrency=2,
                    verbose=False)
    run = pipe.start("暴雨夜，外卖员把最后一单送到一栋没有人的楼")
    pipe.run(run)
    pipe.store.close()
    return run.run_id


def _db(workspace: Path) -> Path:
    return workspace / "runs" / "test.sqlite3"


@contextmanager
def serving(workspace: Path, **kw) -> Iterator[Tuple[str, object]]:
    httpd, app = make_server(workspace, _db(workspace), port=0, quiet=True, **kw)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", app
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str, *, headers: Dict[str, str] | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _post(url: str, data: bytes = b"{}"):
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ————————————————————————————————————————----
# 读不出去
# ————————————————————————————————————————----


def test_only_the_two_artifact_folders_can_be_read(workspace: Path):
    """白名单是目录级的。`config/`、`.env`、仓库根下的任何东西都不给。"""
    for rel in ("config/qc_rules.yaml", ".env", "run.py", "ai_drama/cli.py",
                "docs/architecture.md"):
        assert api.safe_media_path(workspace, rel) is None, rel


def test_traversal_and_absolute_paths_are_refused(workspace: Path):
    """这是整个文件里最要紧的一条断言。"""
    evil = [
        "../secret.txt",
        "runs/../../secret.txt",
        "runs/run_a/../../../secret.txt",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "runs/run_a/..\\..\\secret.txt",
        "",
        ".",
        "runs/",
    ]
    for rel in evil:
        assert api.safe_media_path(workspace, rel) is None, rel


def test_the_database_and_work_files_are_never_served(workspace: Path):
    """数据库是整个项目的账本；`_work/` 是中间产物，不该出现在界面上。"""
    (workspace / "runs").mkdir(parents=True, exist_ok=True)
    (workspace / "runs" / "ai_drama.sqlite3").write_bytes(b"SQLite format 3")
    work = workspace / "runs" / "run_a" / "scenes" / "sc01" / "_work"
    work.mkdir(parents=True)
    (work / "tmp.mp4").write_bytes(b"x")
    assert api.safe_media_path(workspace, "runs/ai_drama.sqlite3") is None
    assert api.safe_media_path(workspace, "runs/run_a/scenes/sc01/_work/tmp.mp4") is None


def test_the_database_is_refused_even_when_it_has_another_name(workspace: Path):
    """`--db` 可以换成任意名字，所以 deny 是传进去的，不是靠文件名猜的。"""
    db = workspace / "runs" / "我的账本.db"
    (workspace / "runs").mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"x")
    assert api.safe_media_path(workspace, "runs/我的账本.db") is None      # 后缀就挡了
    odd = workspace / "runs" / "ledger"
    odd.write_bytes(b"x")
    assert api.safe_media_path(workspace, "runs/ledger") is not None       # 不叫 .db 就过得去
    assert api.safe_media_path(workspace, "runs/ledger", deny=(odd,)) is None


def test_a_file_that_exists_is_served_and_one_that_does_not_is_not(workspace: Path):
    (workspace / "runs" / "run_a").mkdir(parents=True, exist_ok=True)
    ok = workspace / "runs" / "run_a" / "final_video.mp4"
    ok.write_bytes(b"hello")
    assert api.safe_media_path(workspace, "runs/run_a/final_video.mp4") == ok
    assert api.safe_media_path(workspace, "runs/run_a/nope.mp4") is None


# ————————————————————————————————————————----
# 不许自己编
# ————————————————————————————————————————----


def test_a_missing_file_is_reported_as_missing_not_as_nothing(workspace: Path):
    """`None` = 还没有这个东西；`exists=False` = 记录在、文件没了。

    这两件事的处理方式不一样：前者是"还没跑到"，后者是"磁盘出事了"。
    合成一个，界面上就只剩一张裂图。
    """
    assert api.file_ref(workspace, None) is None
    ghost = api.file_ref(workspace, workspace / "runs" / "gone.mp4")
    assert ghost is not None and ghost["exists"] is False and ghost["size"] == 0
    real = workspace / "runs" / "here.mp4"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"12345")
    ref = api.file_ref(workspace, real)
    assert ref["exists"] is True and ref["size"] == 5
    assert ref["url"].startswith("/media?path=")


def test_a_broken_json_report_does_not_take_the_page_down(workspace: Path):
    """一个坏掉的 cost_report.json 该让那一栏显示"读不出来"，不该整页 500。"""
    (workspace / "runs" / "run_a").mkdir(parents=True, exist_ok=True)
    bad = workspace / "runs" / "run_a" / "qc_report.json"
    bad.write_text("{ 这不是 json", encoding="utf-8")
    assert api._read_json(bad) is None


def test_meta_marks_the_kinds_that_are_off_by_default(workspace: Path):
    """八个形态都要列出来，而且 vision / sfx 要显示成"关"。"""
    meta = api.meta_payload(workspace, _db(workspace))
    kinds = {k["kind"]: k for k in meta["kinds"]}
    assert set(kinds) == {"text", "image", "video", "audio",
                          "t2i", "lipsync", "vision", "sfx"}
    assert kinds["vision"]["off"] is True and kinds["vision"]["optional"] is True
    assert kinds["sfx"]["off"] is True
    assert kinds["video"]["off"] is False


# ————————————————————————————————————————----
# HTTP 那一层
# ————————————————————————————————————————----


def test_the_overlay_cannot_survive_the_hidden_attribute(workspace: Path):
    """这条是**用户报的**："网页打开是一片空白 / 全黑，什么都没有"。

    原因不是配色，是一条级联：`hidden` 在 UA 样式表里只是一条 `display:none`，
    而 `#lightbox{display:flex}` 的选择器优先级更高，把它盖掉了 —— 于是那块
    `rgba(0,0,0,.88)` 的灯箱遮罩从打开页面起就一直盖在整个界面上，
    所有东西都只剩 12% 亮度。headless 截图里我一直以为"这个深色主题就是偏暗"。

    CSS 的级联没法从 Python 里跑，所以这里钉的是**契约**：`[hidden]` 必须被
    一条 `!important` 兜住，而且那个灯箱必须真的用了 `hidden`。
    """
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "[hidden]" in css, "没有兜底的 [hidden]{display:none !important}"
    rule = css.split("[hidden]", 1)[1].split("}", 1)[0]
    assert "display:none" in rule.replace(" ", "") and "!important" in rule
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="lightbox"' in page and "hidden" in page
    # 灯箱是全屏遮罩，靠 hidden 属性开关 —— 它一旦常驻，整页就"看不见"了
    assert "#lightbox{" in css.replace(" ", "")


def test_the_page_and_its_static_files_are_served(workspace: Path):
    with serving(workspace) as (base, _app):
        status, body, headers = _get(base + "/")
        assert status == 200 and b"ai-drama" in body
        assert "text/html" in headers["Content-Type"]
        for name in ("app.js", "style.css"):
            st, bd, hd = _get(f"{base}/{name}")
            assert st == 200 and bd, name
        assert (STATIC_DIR / "index.html").is_file()


def test_api_404s_are_json_with_a_reason(workspace: Path):
    with serving(workspace) as (base, _app):
        for url in ("/api/runs/run_nope", "/api/assets/as_nope",
                    "/api/projects/proj_nope"):
            status, body, headers = _get(base + url)
            assert status == 404, url
            assert "application/json" in headers["Content-Type"]
            assert json.loads(body)["error"]


def test_only_the_two_write_endpoints_exist(workspace: Path):
    """写入口只有"改一镜提示词"和"重跑这一镜"，别的一律没有。

    建 run 这条路故意不开：花钱的那条路只留 CLI 的 run / resume 一条，
    预算闸门和 job_id 台账都在那儿。
    """
    with serving(workspace) as (base, _app):
        status, body = _post(base + "/api/runs")
        assert status == 404
        assert "没有这个接口" in json.loads(body)["error"]

        status, body = _post(base + "/api/runs/run_x/shots/sc01/shot_000/prompt",
                             json.dumps({"field": "action", "text": "x"}).encode())
        assert status == 400
        assert "不允许" in json.loads(body)["error"]

        status, body = _post(base + "/api/runs/run_x/rerun",
                             json.dumps({"labels": []}).encode())
        assert status == 400
        assert "至少给一镜" in json.loads(body)["error"]


def test_put_and_delete_are_still_refused(workspace: Path):
    """没被点名的动词还是 405 —— 这两个没有"顺手也能用"的道理。"""
    with serving(workspace) as (base, _app):
        for method in ("PUT", "DELETE"):
            req = urllib.request.Request(base + "/api/runs", data=b"{}",
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    status = r.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            assert status == 405, method


def test_a_big_body_still_gets_a_clean_answer(workspace: Path):
    """带 64KB 请求体的写请求也得拿到一个 JSON —— 而不是"连接被掐断"。"""
    with serving(workspace) as (base, _app):
        status, body = _post(base + "/api/runs", b"x" * 65536)
        assert status == 404
        assert json.loads(body)["error"]


def test_the_refusal_reads_the_request_body_first(workspace: Path):
    """回 405 之前先把请求体读掉。

    不读就回包，socket 里会留着没被消费的数据，关闭连接时 Windows 会发 RST，
    客户端拿到的是 `ConnectionAbortedError`，看不到那句"这个服务是只读的"。
    这就是全量跑时那条偶发失败的成因：单跑必过、跑久了偶尔挂 ——
    而偶发的测试等于没有测试。

    用原生 socket 直接掐这个行为：头里声称还有 1000 字节 body 没发过来，
    **真读了**的服务端会停下来等，**没读**的会立刻回包。
    """
    with serving(workspace) as (base, _app):
        host, _, port = base.replace("http://", "").partition(":")
        with socket.create_connection((host, int(port)), timeout=10) as s:
            s.sendall(
                b"POST /api/runs HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: 1000\r\n\r\n"
            )
            s.settimeout(0.5)
            with pytest.raises((socket.timeout, TimeoutError)):
                s.recv(1)                      # body 还没发完，服务端不该已经回包
            s.sendall(b"y" * 1000)
            s.settimeout(10)
            assert b"404" in s.recv(4096)


def test_traversal_over_http_is_a_404(workspace: Path):
    secret = workspace.parent / "secret.txt"
    secret.write_text("别读我", encoding="utf-8")
    with serving(workspace) as (base, _app):
        for rel in ("../secret.txt", "runs/../../secret.txt", "/etc/passwd"):
            status, _, _ = _get(base + "/media?path=" + rel)
            assert status == 404, rel


def test_binding_anywhere_but_loopback_needs_an_explicit_flag(workspace: Path):
    """危险的默认值不能靠"用户应该知道"来兜 —— 和 vision / sfx 默认关同理。"""
    with pytest.raises(ValueError) as err:
        make_server(workspace, _db(workspace), host="0.0.0.0", port=0)
    assert "--allow-remote" in str(err.value)


def test_range_requests_work_so_the_video_can_be_scrubbed(workspace: Path):
    run_dir = workspace / "runs" / "run_fake"
    run_dir.mkdir(parents=True, exist_ok=True)
    data = bytes(range(256)) * 4                       # 1024 字节
    (run_dir / "final_video.mp4").write_bytes(data)
    with serving(workspace) as (base, _app):
        status, body, headers = _get(base + "/media?path=runs/run_fake/final_video.mp4")
        assert status == 200 and body == data
        assert headers.get("Accept-Ranges") == "bytes"
        status, body, headers = _get(
            base + "/media?path=runs/run_fake/final_video.mp4",
            headers={"Range": "bytes=10-19"},
        )
        assert status == 206
        assert body == data[10:20]
        assert headers["Content-Range"] == "bytes 10-19/1024"


# ————————————————————————————————————————----
# 一条真的 run
# ————————————————————————————————————————----


@pytest.mark.ffmpeg
def test_a_run_exposes_its_storyboard_and_the_files_behind_it(
    workspace: Path, need_ffmpeg: None
):
    run_id = _run_pipeline(workspace)
    store = Store(_db(workspace))
    try:
        payload = api.run_payload(workspace, store, run_id)
    finally:
        store.close()
    assert payload is not None
    sb = payload["storyboard"]
    # 分镜是照 episode.json 原样搬的，不在这里重算 —— 数对不上就说明搬错了
    assert sb["shots_total"] == sum(len(s["shots"]) for s in sb["scenes"]) > 0
    assert sb["scenes"][0]["shots"]
    shot = sb["scenes"][0]["shots"][0]
    # 提示词要带出来 —— 页面上那几个可复制的文本框读的就是这几个字段，
    # 少一个，用户就看到一个空框（或者根本看不到那一栏）。
    for key in ("image_prompt", "last_frame", "video_prompt", "end_action",
                "negative_prompt", "video_negative"):
        assert key in shot, f"分镜没把 {key} 带出去"
    assert shot["negative_prompt"], "负向词是空的话，页面上那个框就是摆设"
    assert shot["video_negative"], "视频负向词同样要带出去"
    assert shot["image_prompt"], "关键帧都渲好了，首帧提示词不该是空的"
    assert shot["last_frame"], "尾帧至少会退化成 action，不该是空字符串"
    # 定妆照那一段在角色卡上，不在分镜上 —— 它是"定这张脸"的提示词，
    # 一集一张，不能跟着每一镜重复一遍。
    assert sb["characters"], "这一集有出镜角色，卡司不该是空的"
    assert sb["characters"][0]["anchor_prompt"], "定妆照提示词没带出去，角色卡上就是空框"
    assert shot["keyframe"]["exists"] is True
    # 路径是 quote 过的（文件名里可能有空格与汉字），所以这里不能写死 runs/
    assert shot["keyframe"]["url"].startswith("/media?path=")
    assert urllib.parse.unquote(shot["keyframe"]["url"]) == \
        "/media?path=" + shot["keyframe"]["rel"]
    assert shot["clip"]["exists"] is True
    assert payload["files"]["final_video"]["exists"] is True
    assert payload["timeline"] and payload["subtitles"]["srt"]
    assert payload["qc"]["state"] in {"pass", "fail", "unchecked"}


@pytest.mark.ffmpeg
def test_deleting_an_artifact_shows_up_instead_of_being_papered_over(
    workspace: Path, need_ffmpeg: None
):
    """这条是这一层的良心：文件没了就得说没了。"""
    run_id = _run_pipeline(workspace)
    paths.timeline(workspace, run_id).unlink()
    store = Store(_db(workspace))
    try:
        payload = api.run_payload(workspace, store, run_id)
    finally:
        store.close()
    assert payload["timeline"] is None
    assert payload["files"]["timeline"]["exists"] is False


@pytest.mark.ffmpeg
def test_assets_and_lineage_are_reachable_from_the_page(
    workspace: Path, need_ffmpeg: None
):
    run_id = _run_pipeline(workspace)
    store = Store(_db(workspace))
    try:
        assets = api.assets_payload(workspace, store, run_id)
        assert assets["summary"]["total"] > 0 and assets["summary"]["edges"] > 0
        kinds = {a["kind"] for a in assets["assets"]}
        assert {"keyframe", "clip", "final"} <= kinds
        final = next(a for a in assets["assets"] if a["kind"] == "final")
        detail = api.asset_payload(workspace, store, final["asset_id"])
    finally:
        store.close()
    assert detail is not None
    # 成片的直接上游是**场次拼接产物**，不是单镜片段：视频轨确实是从那儿来的
    assert any(u["asset"]["kind"] == "scene" for u in detail["upstream"])
    assert detail["tree"][0]["depth"] == 0
    assert detail["asset"]["file"]["exists"] is True


@pytest.mark.ffmpeg
def test_the_asset_summary_has_the_shape_the_page_reads(
    workspace: Path, need_ffmpeg: None
):
    """这条是浏览器那一遍抓出来的：by_kind 的值是 `{"count","bytes"}` 而不是数字，
    页面却直接把值印出来，于是六张卡片全写着 `[object Object]`。

    API 测试全绿、页面全错 —— 所以把两边**都要用到的那个形状**钉在这里：
    形状改了，这条会红；页面不再读 `.count`，这条也会红。
    """
    run_id = _run_pipeline(workspace)
    store = Store(_db(workspace))
    try:
        summary = api.assets_summary(workspace, store, run_id)
    finally:
        store.close()
    assert summary["by_kind"], "没有 by_kind 就说明台账是空的"
    for kind, v in summary["by_kind"].items():
        assert isinstance(v, dict), f"{kind} 的值不是一个带 count/bytes 的字典"
        assert isinstance(v["count"], int) and isinstance(v["bytes"], int)
    assert summary["by_kind"]["keyframe"]["count"] >= 1
    page = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "v.count" in page, "页面不再读 .count 了 —— 那几张卡片会印出 [object Object]"


def test_the_storyboard_hands_prompts_over_as_copyable_boxes():
    """提示词是用 textarea 摆出来的，不是一段 <pre>。

    为什么在乎这个：用户要把提示词复制到别的视频工具里去。`<pre>` 双击选不干净、
    也改不了；textarea 能一键全选、能改、能读回 value。这条用例钉的是"别哪天
    被谁顺手改回成 div"。
    """
    page = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "function promptBox" in page
    assert 'class="pftext"' in page, "提示词不再用 textarea 渲染了"
    assert 'onclick="copyField(this)"' in page, "复制按钮不见了"
    # 四段里有一段在角色卡上（定妆照），三段在每一镜上。只顾分镜的话，
    # 用户永远拿不到"定这张脸"的那段话。
    assert "function shotPrompts" in page, "分镜那三段没有走同一个渲染器"
    assert "promptBox(c.anchor_prompt" in page, "角色卡上没摆定妆照提示词"
    # 剪贴板 API 在非 https 的局域网地址上会失败，必须留着 execCommand 的退路
    assert "navigator.clipboard" in page and "execCommand" in page
    # 空字段不该摆一个空框出来假装有内容
    assert ".filter(([, v]) => v)" in page

    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert ".pftext" in css and ".cp" in css, "样式没跟上，框会贴在边上"


def test_the_whole_episode_of_prompts_can_be_taken_in_one_click():
    """整集提示词要能一次拿走，而不是一镜一镜点复制。

    为什么在乎这个：一集几十镜，每镜三段 —— 一个个点，做一集要点上百次，
    还会漏掉最后一镜。导出走 Blob，不碰服务器：页面是只读的，这条不能破。
    """
    page = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "function exportPrompts" in page
    assert 'onclick="exportPrompts()"' in page, "导出按钮不见了"
    # 纯前端下载：不能为了导出在服务端开一个写接口
    assert "createObjectURL" in page and "a.download" in page
    # 四段一段都不能少 —— 少一段就是导了个残的
    assert "anchor_prompt" in page, "定妆照提示词没进导出"
    for seg in ("【首帧】", "【尾帧】", "【运镜】"):
        assert seg in page, f"导出文件里没有 {seg}"
    # 文件名里的 / : * 在 Windows 上会让下载直接失败
    assert '.replace(/[\\\\/:*?"<>|]/g' in page, "文件名没做非法字符替换"
    # 不带 BOM 的 UTF-8，Windows 记事本打开是乱码
    assert "\\ufeff" in page, "导出文件没带 BOM"

    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert ".pbar" in css, "导出按钮会贴在内容上"


def test_the_cost_page_never_lets_fake_money_pass_for_real_money():
    """页面上的钱必须分得清真假，否则 run 列表和成本页都在说假话。

    dryrun 的价是编的，一分钱没出门。混成一个数念，只产提示词的用户会
    以为被扣了钱 —— 而账单是这套工具里唯一会让人紧张的数字。
    """
    page = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "function moneyLabel" in page, "run 列表还在直接念 total"
    assert "simulated_money" in page and "real_money" in page
    assert "虚拟" in page, "占位估算没有标出来"
    assert "by_stage_simulated" in page and "by_provider_simulated" in page
    assert "没有真的扣费" in page, "总计行没解释那笔钱是编的"


@pytest.mark.ffmpeg
def test_the_whole_page_survives_a_real_run(workspace: Path, need_ffmpeg: None):
    """端到端把每个 API 都打一遍：一个 500 都不许有。"""
    run_id = _run_pipeline(workspace)
    with serving(workspace) as (base, _app):
        for url in ("/api/meta", "/api/runs", f"/api/runs/{run_id}",
                    f"/api/runs/{run_id}/assets", "/api/projects"):
            status, body, headers = _get(base + url)
            assert status == 200, (url, status, body[:200])
            assert "application/json" in headers["Content-Type"]
            json.loads(body)
        status, body, _ = _get(base + f"/api/runs/{run_id}")
        payload = json.loads(body)
        kf = payload["storyboard"]["scenes"][0]["shots"][0]["keyframe"]["rel"]
        status, body, _ = _get(base + "/media?path=" + kf)
        assert status == 200 and body[:4] == b"\x89PNG"
