"""网页上改提示词、重跑这一镜。

这一类功能最容易出的错不是"报错",而是"看起来生效了":改了文本框、页面显示
新值、重跑出来的画面还是旧的。根因都在同一条链上 —— 数据有两个副本
(`episode.json` 和 sqlite),resume 时 Pipeline 从库里取 episode。所以这里
每一条都验两边,而不只是返回值。
"""

from __future__ import annotations

import contextlib
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, Tuple

import pytest

from ai_drama.core import editing, paths
from ai_drama.core.pipeline import Pipeline
from ai_drama.core.store import Store
from ai_drama.providers import registry


@pytest.fixture()
def made(workspace: Path):
    """一条只跑到分镜的 run：有 episode.json，也没花钱。"""
    store = Store(workspace / "runs" / "test.sqlite3")
    pipe = Pipeline(workspace, store, registry.build_bundle(),
                    concurrency=1, verbose=False)
    run = pipe.start("深夜回家发现门锁被换过", scenes=1, shots_per_scene=2)
    pipe.run(run, stages=["script", "storyboard"])
    yield workspace, store, run.run_id
    store.close()


def _episode(root: Path, run_id: str) -> dict:
    return json.loads(paths.episode_json(root, run_id).read_text(encoding="utf-8"))


def test_editing_a_prompt_lands_in_both_copies(made):
    root, store, run_id = made
    label = "sc01/shot_000"

    shot = editing.update_prompt(root, run_id, label, "image_prompt",
                                 "  雨夜，猫坐在窗台上  ", store=store)
    assert shot["image_prompt"] == "雨夜，猫坐在窗台上"      # 两端空白被剪掉

    # 第一份：磁盘上的 episode.json
    data = _episode(root, run_id)
    assert data["scenes"][0]["shots"][0]["image_prompt"] == "雨夜，猫坐在窗台上"

    # 第二份：库（resume 真正读的那一份）
    ep = store.get_episode(run_id)
    assert ep.shots[0].image_prompt == "雨夜，猫坐在窗台上"


def test_every_prompt_field_can_be_edited(made):
    root, store, run_id = made
    for field in editing.PROMPT_FIELDS:
        editing.update_prompt(root, run_id, "sc01/shot_001", field, f"值-{field}",
                              store=store)
    data = _episode(root, run_id)
    body = data["scenes"][0]["shots"][1]
    for field in editing.PROMPT_FIELDS:
        assert body[field] == f"值-{field}"


def test_structure_fields_are_not_editable(made):
    """动作、台词、出镜人改了要牵连景别/参考图/时长，不是网页上能安全做的事。"""
    root, store, run_id = made
    for field in ("action", "dialogue", "characters_in_shot", "duration"):
        with pytest.raises(editing.EditError) as err:
            editing.update_prompt(root, run_id, "sc01/shot_000", field, "x",
                                  store=store)
        assert err.value.code == 400
        assert "不允许" in err.value.message


def test_unknown_shot_is_a_404(made):
    root, store, run_id = made
    with pytest.raises(editing.EditError) as err:
        editing.update_prompt(root, run_id, "sc09/shot_042", "image_prompt", "x",
                              store=store)
    assert err.value.code == 404


def test_too_long_is_refused_before_writing_anything(made):
    root, store, run_id = made
    before = paths.episode_json(root, run_id).read_text(encoding="utf-8")
    with pytest.raises(editing.EditError) as err:
        editing.update_prompt(root, run_id, "sc01/shot_000", "image_prompt",
                              "字" * (editing.MAX_CHARS + 1), store=store)
    assert err.value.code == 400
    assert "太长" in err.value.message
    assert paths.episode_json(root, run_id).read_text(encoding="utf-8") == before


def test_a_broken_episode_json_is_reported_not_overwritten(workspace: Path):
    run_dir = workspace / "runs" / "run_broken"
    run_dir.mkdir(parents=True)
    paths.episode_json(workspace, "run_broken").write_text("{ 不是 json",
                                                          encoding="utf-8")
    with pytest.raises(editing.EditError) as err:
        editing.update_prompt(workspace, "run_broken", "sc01/shot_000",
                              "image_prompt", "x")
    assert err.value.code == 409
    # 坏文件原样留着 —— 覆盖掉等于把用户手改的内容扔了
    assert paths.episode_json(workspace, "run_broken").read_text(
        encoding="utf-8") == "{ 不是 json"


def test_only_the_named_shot_is_rerun(workspace: Path):
    """`only_shots` 不是"过滤报告"，是真的不去跑另外那些镜。"""
    store = Store(workspace / "runs" / "test.sqlite3")
    pipe = Pipeline(workspace, store, registry.build_bundle(),
                    concurrency=1, verbose=False)
    run = pipe.start("深夜回家发现门锁被换过", scenes=1, shots_per_scene=3)
    pipe.run(run, stages=["script", "storyboard", "frames"])

    def mtimes() -> dict:
        out = {}
        for shot in store.get_episode(run.run_id).shots:
            p = paths.keyframe(workspace, run.run_id, shot.scene_id, shot.order)
            out[shot.label] = p.stat().st_mtime_ns if p.exists() else None
        return out

    before = mtimes()
    assert all(v is not None for v in before.values())

    editing.rerun_shots(workspace, run.run_id, ["sc01/shot_001"], store=store)
    after = mtimes()

    assert after["sc01/shot_001"] != before["sc01/shot_001"]   # 这一镜真重跑了
    assert after["sc01/shot_000"] == before["sc01/shot_000"]
    assert after["sc01/shot_002"] == before["sc01/shot_002"]
    store.close()


@contextlib.contextmanager
def _serve(root: Path, db: Path) -> Iterator[str]:
    from ai_drama.web.app import make_server
    httpd, _app = make_server(root, db, port=0, quiet=True)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _post(url: str, payload: dict) -> Tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_page_can_save_a_prompt_over_http(made):
    root, store, run_id = made
    db = store.path if hasattr(store, "path") else root / "runs" / "test.sqlite3"
    with _serve(root, db) as base:
        status, body = _post(
            f"{base}/api/runs/{run_id}/shots/sc01/shot_000/prompt",
            {"field": "last_frame", "text": "猫抬头看向窗外"})
        assert status == 200, body
        assert body["shot"]["last_frame"] == "猫抬头看向窗外"

        # 页面再读一次，看到的必须是刚存的那句
        with urllib.request.urlopen(f"{base}/api/runs/{run_id}", timeout=20) as r:
            payload = json.loads(r.read())
    shots = payload["storyboard"]["scenes"][0]["shots"]
    assert shots[0]["last_frame"] == "猫抬头看向窗外"
