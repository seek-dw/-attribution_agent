"""2.1.10 八类实时消息的字段完整性。

此前只验证了"消息类型能收到"，没验证原文要求的字段是否存在——
导致 task_id 缺失这类缺陷漏网。这里逐类断言最小字段集。
"""

from __future__ import annotations

import time

import pytest

# 原文 2.1.10 的最小字段要求
REQUIRED_FIELDS: dict[str, set[str]] = {
    "message_start": {"task_id", "conversation_id"},
    "message_delta": {"task_id", "delta_text"},
    "tool_start": {"task_id", "tool_name"},
    "tool_finish": {"task_id", "tool_name", "tool_result_summary"},
    "task_status": {"task_id", "task_status", "current_step"},
    "result_ready": {"task_id", "result_id"},
    "error": {"task_id", "error_message"},
    "done": {"task_id", "finished_at"},
}


def _run_task_and_collect(auth_client, cid: int, question: str) -> dict[str, list[dict]]:
    """跑一次分析并经 WebSocket 收集消息。"""
    tok = auth_client.post("/api/chat/ws-token",
                           json={"conversation_id": cid}).json()
    collected: dict[str, list[dict]] = {}

    with auth_client.websocket_connect(
            f"/api/chat/ws/chat?websocket_token={tok['websocket_token']}"
            f"&conversation_id={cid}") as ws:
        ws.receive_json()  # 首帧：task_status 回放
        auth_client.post("/api/chat/send",
                         json={"conversation_id": cid, "text": question})
        t0 = time.time()
        while time.time() - t0 < 90:
            try:
                m = ws.receive_json()
            except Exception:
                break
            collected.setdefault(m.get("type", "?"), []).append(m)
            if m.get("type") == "done":
                break
    return collected


def test_message_fields_match_spec(auth_client):
    cid = auth_client.post("/api/chat/create",
                           json={"title": "消息字段"}).json()["conversation_id"]
    got = _run_task_and_collect(
        auth_client, cid, "为什么 2024-06 的销售额下滑了？")

    assert got, "未收到任何消息"
    problems: list[str] = []
    for mtype, min_fields in REQUIRED_FIELDS.items():
        items = got.get(mtype, [])
        if not items:
            # error 只在失败时发出，允许本轮缺席
            if mtype == "error":
                continue
            problems.append(f"{mtype}: 未收到")
            continue
        for i, m in enumerate(items):
            lack = min_fields - set(m.keys())
            if lack:
                problems.append(
                    f"{mtype}[{i}] 缺字段 {sorted(lack)}，实际 keys={sorted(m.keys())}")

    assert not problems, "消息字段不符合 2.1.10：\n" + "\n".join(problems)


def test_tool_messages_carry_tool_name(auth_client):
    """tool_start / tool_finish 的 tool_name 必须与实际执行的工具一致。"""
    cid = auth_client.post("/api/chat/create",
                           json={"title": "工具名一致性"}).json()["conversation_id"]
    got = _run_task_and_collect(
        auth_client, cid, "为什么 2024-06 的销售额下滑了？")

    starts = {m["tool_name"] for m in got.get("tool_start", [])}
    finishes = {m["tool_name"] for m in got.get("tool_finish", [])}
    assert starts, "应有 tool_start"
    assert starts == finishes, f"start/finish 的工具名不一致：{starts} vs {finishes}"

    # 与落库的工具调用记录对账
    task_id = got["message_start"][0]["task_id"]
    rows = auth_client.get(f"/api/tasks/{task_id}/tools").json()
    db_names = {r["tool_name"] for r in rows}
    assert db_names == starts, f"WS 广播与落库记录不一致：{db_names} vs {starts}"


def test_task_status_carries_real_status(auth_client):
    """task_status 消息里的 task_status 字段必须是真实状态值。"""
    cid = auth_client.post("/api/chat/create",
                           json={"title": "状态字段"}).json()["conversation_id"]
    got = _run_task_and_collect(
        auth_client, cid, "为什么 2024-06 的销售额下滑了？")

    statuses = {m.get("task_status") for m in got.get("task_status", [])}
    assert statuses, "应有 task_status 消息"
    assert statuses <= {"queued", "running", "success", "failed", "cancelled"}, \
        f"task_status 出现了枚举外的值：{statuses}"
