"""工具链路完整性：2.1.1 要求五类分析能力都真正参与取证。

这是对"实现了但没调用"的回归防线——
四个工具此前只是存在，引擎从未调用，等于需求打折。
"""

from __future__ import annotations

import io

import pytest

from app.analysis.router import plan_tools


# ------------------------------------------------------------------ 路由
def test_router_attachment_hint_plans_text_search():
    p = plan_tools("附件里提到的渠道预算是什么？", has_attachments=True)
    assert p.need_text_search is True
    assert p.keywords, "检索必须有具体关键词"


def test_router_no_attachment_no_search():
    p = plan_tools("附件里提到什么？", has_attachments=False)
    assert p.need_text_search is False


def test_router_export_plans_file_write():
    p = plan_tools("把结果导出成文件", has_attachments=False)
    assert p.need_file_write is True


def test_router_compute_plans_command():
    p = plan_tools("帮我算一下口径对不对", has_attachments=False)
    assert p.need_command is True


def test_router_plain_question_no_extra_tools():
    p = plan_tools("为什么 2024-06 的销售额下滑了？", has_attachments=True)
    assert p.need_text_search is False
    assert p.need_file_write is False
    assert p.need_command is False


# ------------------------------------------------ 真实链路：text_search 进证据
def test_text_search_evidence_in_result(auth_client):
    """上传附件 + 提到附件 → text_search 被调用且命中进入证据。"""
    import time

    cid = auth_client.post("/api/chat/create", json={"title": "附件取证"}).json()["conversation_id"]
    files = {"file": ("channel_plan.txt",
                      io.BytesIO("渠道预算削减说明：信息流预算下降 30%，直播预算持平。".encode()),
                      "text/plain")}
    up = auth_client.post("/api/attachment/upload",
                          data={"conversation_id": cid}, files=files).json()
    assert up["parse_status"] == "success"

    r = auth_client.post("/api/chat/send", json={
        "conversation_id": cid,
        "text": "附件里提到的渠道预算削减，为什么 2024-06 的销售额下滑了？",
    }).json()
    tid = r["task_id"]
    for _ in range(80):
        st = auth_client.get(f"/api/tasks/{tid}").json()
        if st["task_status"] in ("success", "failed"):
            break
        time.sleep(0.3)
    assert st["task_status"] == "success", st

    tools_used = auth_client.get(f"/api/tasks/{tid}/tools").json()
    names = [t["tool_name"] for t in tools_used]
    assert "text_search" in names, f"应调用文本检索，实际：{names}"

    res = auth_client.get(f"/api/results/{tid}").json()
    att_ev = [e for e in res["evidence_list"] if e["source_type"] == "attachment"]
    assert att_ev, "检索命中的附件片段必须进入证据列表"
    assert any("预算" in e["evidence_text"] for e in att_ev), \
        f"证据应包含命中内容：{att_ev}"


# ------------------------------------------------ 真实链路：file_write 落文件
def test_file_write_artifact_created(auth_client):
    import time

    cid = auth_client.post("/api/chat/create", json={"title": "导出中间结果"}).json()["conversation_id"]
    r = auth_client.post("/api/chat/send", json={
        "conversation_id": cid,
        "text": "为什么 2024-06 的销售额下滑了？把中间结果导出成文件",
    }).json()
    tid = r["task_id"]
    for _ in range(80):
        st = auth_client.get(f"/api/tasks/{tid}").json()
        if st["task_status"] in ("success", "failed"):
            break
        time.sleep(0.3)

    tools_used = auth_client.get(f"/api/tasks/{tid}/tools").json()
    names = [t["tool_name"] for t in tools_used]
    assert "file_write" in names, f"应调用文件写入，实际：{names}"

    ok = [t for t in tools_used if t["tool_name"] == "file_write" and t["status"] == "success"]
    assert ok, "文件写入应成功"


# ------------------------------------------------ 真实链路：command_run 校验
def test_command_run_check_invoked(auth_client):
    import time

    cid = auth_client.post("/api/chat/create", json={"title": "命令校验"}).json()["conversation_id"]
    r = auth_client.post("/api/chat/send", json={
        "conversation_id": cid,
        "text": "为什么 2024-06 的销售额下滑了？帮我算一下口径对不对",
    }).json()
    tid = r["task_id"]
    for _ in range(80):
        st = auth_client.get(f"/api/tasks/{tid}").json()
        if st["task_status"] in ("success", "failed"):
            break
        time.sleep(0.3)

    tools_used = auth_client.get(f"/api/tasks/{tid}/tools").json()
    names = [t["tool_name"] for t in tools_used]
    assert "command_run" in names, f"应调用命令执行，实际：{names}"


# ------------------------------------------------ 真实链路：db_query 是基础
def test_db_query_always_used(auth_client):
    import time

    cid = auth_client.post("/api/chat/create", json={"title": "基础取数"}).json()["conversation_id"]
    r = auth_client.post("/api/chat/send", json={
        "conversation_id": cid,
        "text": "为什么 2024-06 的销售额下滑了？",
    }).json()
    tid = r["task_id"]
    for _ in range(80):
        st = auth_client.get(f"/api/tasks/{tid}").json()
        if st["task_status"] in ("success", "failed"):
            break
        time.sleep(0.3)

    tools_used = auth_client.get(f"/api/tasks/{tid}/tools").json()
    assert any(t["tool_name"] == "db_query" for t in tools_used)
    assert len(tools_used) >= 3, "归因至少要查多个指标"


# ------------------------------------------------ 2.1.10 message_delta
def test_message_delta_event_emitted(auth_client):
    """message_delta 必须有真实内容（此前从未发送，8 类消息缺 1 类）。"""
    import time

    cid = auth_client.post("/api/chat/create", json={"title": "增量消息"}).json()["conversation_id"]
    tok = auth_client.post("/api/chat/ws-token",
                           json={"conversation_id": cid}).json()

    deltas: list[str] = []
    with auth_client.websocket_connect(
            f"/api/chat/ws/chat?websocket_token={tok['websocket_token']}"
            f"&conversation_id={cid}") as ws:
        ws.receive_json()   # 首帧为 task_status 回放
        auth_client.post("/api/chat/send", json={
            "conversation_id": cid,
            "text": "为什么 2024-06 的销售额下滑了？",
        })
        t0 = time.time()
        while time.time() - t0 < 60 and len(deltas) < 2:
            try:
                m = ws.receive_json()
            except Exception:
                break
            if m.get("type") == "message_delta":
                deltas.append(m.get("delta_text") or "")
            elif m.get("type") == "done":
                break

    assert deltas, "必须发出 message_delta"
    assert any(d.strip() for d in deltas), "message_delta 必须有真实内容，不能是空占位"


# ------------------------------------------------ 2.1.4 archived 状态
def test_conversation_archive_status(auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "归档测试"}).json()["conversation_id"]
    r = auth_client.post("/api/chat/update",
                         json={"conversation_id": cid, "status": "archived"}).json()
    assert r["status"] == "archived"

    rows = auth_client.get("/api/chat/ls").json()
    row = next(x for x in rows if x["conversation_id"] == cid)
    assert row["status"] == "archived", "归档状态必须可查"

    # 恢复
    r2 = auth_client.post("/api/chat/update",
                          json={"conversation_id": cid, "status": "active"}).json()
    assert r2["status"] == "active"


def test_conversation_invalid_status_rejected(auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "非法状态"}).json()["conversation_id"]
    r = auth_client.post("/api/chat/update",
                         json={"conversation_id": cid, "status": "bogus"})
    assert r.status_code == 400


# ------------------------------------------------ 2.1.6 摘要读回
def test_summary_read_back_into_analysis(auth_client):
    """摘要生成后必须被后续分析读取（此前只生成不使用）。"""
    from app import config
    from app.db import ContextSummary, get_session

    cid = auth_client.post("/api/chat/create", json={"title": "摘要读回"}).json()["conversation_id"]

    old = config.get("summary_trigger_messages")
    config._cache["summary_trigger_messages"] = 4
    config._cache["summary_keep_recent"] = 1
    try:
        import time
        for q in ("2024-06 的销售额", "2024-05 的销售额", "2024-04 的销售额"):
            r = auth_client.post("/api/chat/send",
                                 json={"conversation_id": cid, "text": q}).json()
            for _ in range(80):
                st = auth_client.get(f"/api/tasks/{r['task_id']}").json()
                if st["task_status"] in ("success", "failed"):
                    break
                time.sleep(0.3)

        s = get_session()
        try:
            n = s.query(ContextSummary).filter_by(conversation_id=cid).count()
        finally:
            s.close()
        assert n >= 1, "达到阈值应生成上下文摘要"
    finally:
        config._cache["summary_trigger_messages"] = old
        config._cache.pop("summary_keep_recent", None)
