"""MCP 入口：agent 调这一层，而不是去爬网页。

两条硬约束在测试里钉住：**stdout 只有 JSON-RPC**（混进一行阶段报告，客户端就
解不开），以及**工具失败不能让服务死**（回 isError，进程继续读下一行）。
"""

from __future__ import annotations

import io
import json

import pytest

from ai_drama import mcp_server


def _call(name: str, arguments: dict) -> dict:
    response = mcp_server.handle({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response is not None
    return response


def test_initialize_announces_the_server_and_a_protocol_version():
    response = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {}})
    result = response["result"]
    assert result["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "ai-drama"
    assert "tools" in result["capabilities"]


def test_notifications_get_no_answer():
    assert mcp_server.handle({"jsonrpc": "2.0",
                              "method": "notifications/initialized"}) is None


def test_tools_list_names_every_tool_with_a_schema():
    result = mcp_server.handle({"jsonrpc": "2.0", "id": 2,
                                "method": "tools/list"})["result"]
    by_name = {t["name"]: t for t in result["tools"]}
    assert set(by_name) == {"write_prompts", "lint_prompts", "list_runs"}
    assert by_name["write_prompts"]["inputSchema"]["required"] == ["idea"]
    assert "负向" in by_name["write_prompts"]["description"]


def test_write_prompts_returns_a_bundle_and_never_touches_an_image_model(workspace, monkeypatch):
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("AI_DRAMA_TEXT", "dryrun")
    monkeypatch.setenv("AI_DRAMA_SHOT_SECONDS", "3")
    response = _call("write_prompts", {"idea": "深夜书店里有人敲门", "scenes": 1,
                                       "shots_per_scene": 1})
    text = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is False
    bundle = json.loads(text)
    assert bundle["scenes"] and bundle["characters"]
    shot = bundle["scenes"][0]["shots"][0]
    for field in ("image_prompt", "last_frame", "video_prompt", "negative_prompt"):
        assert shot[field]

    # 同一份东西能从诊断工具读回来
    linted = json.loads(_call("lint_prompts",
                              {"run_id": bundle["run_id"]})["result"]["content"][0]["text"])
    assert linted["run_id"] == bundle["run_id"]
    assert isinstance(linted["shots"][0]["issues"], list)


def test_an_unknown_tool_is_an_error_not_a_crash():
    response = mcp_server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "make_coffee", "arguments": {}},
    })
    assert response["error"]["code"] == -32602
    assert "make_coffee" in response["error"]["message"]


def test_a_failing_tool_comes_back_as_is_error(workspace, monkeypatch):
    monkeypatch.chdir(workspace)
    response = _call("lint_prompts", {"run_id": "run_不存在"})
    assert response["result"]["isError"] is True
    assert "run_不存在" in response["result"]["content"][0]["text"]


def test_stdout_carries_only_protocol_lines(workspace, monkeypatch):
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("AI_DRAMA_TEXT", "dryrun")
    monkeypatch.setenv("AI_DRAMA_SHOT_SECONDS", "3")
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "list_runs", "arguments": {}}}),
        "这不是 json",
    ]
    out = io.StringIO()
    code = mcp_server.serve(io.StringIO("\n".join(lines) + "\n"), out)
    assert code == 0
    written = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    for item in written:
        assert item["jsonrpc"] == "2.0"
    assert [item.get("id") for item in written] == [1, 2, 3, None]   # 通知不占位
    assert written[-1]["error"]["code"] == -32700
