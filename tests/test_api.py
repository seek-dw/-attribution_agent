"""AC-1~AC-8、AC-14~AC-16：接口层行为。"""

from __future__ import annotations

import io
import time

import pytest


# ------------------------------------------------------------------ AC-1 认证
def test_ac1_unauth_redirects_to_login(client):
    r = client.get("/workbench", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login"


def test_ac1_login_page_has_no_business_data(client):
    r = client.get("/auth/login")
    assert r.status_code == 200
    body = r.text
    assert "登录" in body
    assert "会话" not in body


def test_ac1_business_api_requires_login(client):
    for path in ("/api/chat/ls", "/api/admin/config", "/api/admin/logs",
                 "/api/admin/rejections"):
        r = client.get(path)
        assert r.status_code == 401, f"{path} 应要求登录"


def test_ac1_wrong_password_rejected(client):
    r = client.post("/auth/login", data={"username": "analyst",
                                         "password": "wrong"})
    assert r.status_code == 401


def test_ac1_callback_establishes_session(client):
    r = client.get("/auth/callback?code=analyst", follow_redirects=False)
    assert r.status_code == 303
    assert "attr_session" in r.cookies or r.cookies.get("attr_session")


def test_ac1_admin_only_endpoints(auth_client):
    r = auth_client.get("/api/admin/config")
    assert r.status_code == 403, "分析用户不得访问管理员接口"


def test_ac1_me(auth_client):
    r = auth_client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "analyst"


# ------------------------------------------------------------------ AC-2 会话
def test_ac2_create_list_rename_delete(auth_client):
    c = auth_client.post("/api/chat/create", json={"title": "测试会话"}).json()
    cid = c["conversation_id"]

    rows = auth_client.get("/api/chat/ls").json()
    assert any(r["conversation_id"] == cid for r in rows)

    u = auth_client.post("/api/chat/update",
                         json={"conversation_id": cid, "title": "改名后"}).json()
    assert u["title"] == "改名后"

    d = auth_client.post("/api/chat/delete",
                         json={"conversation_ids": [cid]}).json()
    assert cid in d["deleted"]

    rows = auth_client.get("/api/chat/ls").json()
    assert not any(r["conversation_id"] == cid for r in rows)


def test_ac2_cross_user_conversation_blocked(client, auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "私有"}).json()["conversation_id"]
    # 换一个用户
    r = client.post("/auth/login", data={"username": "other", "password": "other123"},
                    follow_redirects=False)
    assert r.status_code == 303
    r2 = client.get(f"/api/chat/ls/{cid}")
    assert r2.status_code == 404, "不得读取他人会话"


# ------------------------------------------------------------------ AC-2 消息顺序
def test_ac2_message_seq_strictly_increasing(auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "顺序"}).json()["conversation_id"]
    auth_client.post("/api/chat/send", json={"conversation_id": cid, "text": "销售额怎么样？"})
    time.sleep(1.2)
    auth_client.post("/api/chat/send", json={"conversation_id": cid, "text": "2024-06 的销售额"})
    time.sleep(1.2)

    msgs = auth_client.get(f"/api/chat/ls/{cid}").json()
    seqs = [m["seq_no"] for m in msgs]
    assert seqs == sorted(seqs), "消息必须按顺序号返回"
    assert len(seqs) == len(set(seqs)), "顺序号不得重复"


# ------------------------------------------------------------------ AC-14 单任务
def test_ac14_concurrent_task_rejected(auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "并发"}).json()["conversation_id"]
    r1 = auth_client.post("/api/chat/send",
                          json={"conversation_id": cid,
                                "text": "为什么 2024-06 的销售额下滑了？"})
    assert r1.status_code == 200
    r2 = auth_client.post("/api/chat/send",
                          json={"conversation_id": cid, "text": "再问一次"})
    assert r2.status_code == 409, "同一会话不得并发两个任务"


# ------------------------------------------------------------------ AC-3 附件
def test_ac3_upload_list_download_delete(auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "附件"}).json()["conversation_id"]
    files = {"file": ("note.txt", io.BytesIO("渠道预算说明".encode()), "text/plain")}
    up = auth_client.post("/api/attachment/upload",
                          data={"conversation_id": cid}, files=files).json()
    assert up["parse_status"] == "success"
    aid = up["attachment_id"]

    rows = auth_client.get(f"/api/attachments/{cid}").json()
    assert any(r["attachment_id"] == aid for r in rows)
    assert rows[0]["file_name"] == "note.txt"
    assert rows[0]["file_size"] > 0

    g = auth_client.get(f"/api/attachment/get?attachment_id={aid}")
    assert g.status_code == 200

    d = auth_client.post("/api/attachment/delete",
                         json={"attachment_id": aid}).json()
    assert d["deleted"] is True


def test_ac3_attachment_cross_user_blocked(client, auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "附件越权"}).json()["conversation_id"]
    files = {"file": ("a.txt", io.BytesIO(b"x"), "text/plain")}
    aid = auth_client.post("/api/attachment/upload",
                           data={"conversation_id": cid}, files=files).json()["attachment_id"]

    client.post("/auth/login", data={"username": "other", "password": "other123"},
                follow_redirects=False)
    r = client.get(f"/api/attachment/get?attachment_id={aid}")
    assert r.status_code == 403, "不得下载他人附件"


# ------------------------------------------------------------------ AC-16 WS 令牌
def test_ac16_ws_token_one_time(auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "WS"}).json()["conversation_id"]
    tok = auth_client.post("/api/chat/ws-token",
                           json={"conversation_id": cid}).json()

    with auth_client.websocket_connect(
            f"/api/chat/ws/chat?websocket_token={tok['websocket_token']}"
            f"&conversation_id={cid}") as ws:
        first = ws.receive_json()
        assert first["type"] in ("task_status", "ping")

    # 第二次必须被拒（4401）
    with pytest.raises(Exception):
        with auth_client.websocket_connect(
                f"/api/chat/ws/chat?websocket_token={tok['websocket_token']}"
                f"&conversation_id={cid}") as ws2:
            ws2.receive_json()


def test_ac16_ws_rejects_unauthenticated(auth_client):
    with pytest.raises(Exception):
        with auth_client.websocket_connect(
                "/api/chat/ws/chat?websocket_token=fake&conversation_id=1") as ws:
            ws.receive_json()


# ------------------------------------------------------------------ AC-7 配置
def test_ac7_reload_takes_effect(admin_client):
    before = admin_client.get("/api/admin/config").json()
    r = admin_client.post("/api/admin/reload",
                          json={"updates": {"delta_threshold_pct": 3.5}})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    after = admin_client.get("/api/admin/config").json()
    assert after["delta_threshold_pct"] == 3.5
    assert before["delta_threshold_pct"] != after["delta_threshold_pct"]

    # 改回
    admin_client.post("/api/admin/reload",
                      json={"updates": {"delta_threshold_pct": 5.0}})


def test_ac7_config_reload_reports_changes(admin_client):
    r = admin_client.post("/api/admin/reload",
                          json={"updates": {"task_max_steps": 25}}).json()
    assert "task_max_steps" in r["changed"]
    admin_client.post("/api/admin/reload", json={"updates": {"task_max_steps": 30}})


# ------------------------------------------------------------------ AC-2 完整链路
def test_ac2_full_analysis_flow(auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "端到端"}).json()["conversation_id"]
    r = auth_client.post("/api/chat/send",
                         json={"conversation_id": cid,
                               "text": "为什么 2024-06 的销售额下滑了？"}).json()
    tid = r["task_id"]

    for _ in range(60):
        st = auth_client.get(f"/api/tasks/{tid}").json()
        if st["task_status"] in ("success", "failed", "cancelled"):
            break
        time.sleep(0.3)

    assert st["task_status"] == "success", st

    res = auth_client.get(f"/api/results/{tid}").json()
    assert res["problem_definition"]
    assert res["key_metrics"]
    assert res["evidence_list"]
    assert res["conclusion_text"]
    assert len(res["hypotheses"]) >= 2, "AC-19：因果问题至少 2 个假设"

    dl = auth_client.get(f"/api/results/{tid}/download")
    assert dl.status_code == 200
    assert "经营归因分析报告" in dl.text

    tools_used = auth_client.get(f"/api/tasks/{tid}/tools").json()
    assert tools_used, "工具调用必须留痕（FR-6.3）"


def test_ac5_clarification_flow_persisted(auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "澄清"}).json()["conversation_id"]
    r = auth_client.post("/api/chat/send",
                         json={"conversation_id": cid, "text": "销售额怎么样？"}).json()
    tid = r["task_id"]
    for _ in range(60):
        st = auth_client.get(f"/api/tasks/{tid}").json()
        if st["task_status"] in ("success", "failed"):
            break
        time.sleep(0.3)

    st_state = auth_client.get(f"/api/state/{cid}").json()
    assert st_state["pending_question"], "澄清问题必须持久化（FR-5.7）"

    msgs = auth_client.get(f"/api/chat/ls/{cid}").json()
    assert any(m["message_type"] == "clarify" for m in msgs)


# ------------------------------------------------------------------ AC-15 删除级联
def test_ac15_delete_cascade_no_residue(auth_client):
    from app import config
    from app.db import (AnalysisResult, AnalysisTask, Attachment, ContextSummary,
                        Message, get_session)
    from app.db import Conversation

    cid = auth_client.post("/api/chat/create", json={"title": "级联"}).json()["conversation_id"]
    files = {"file": ("x.txt", io.BytesIO(b"data"), "text/plain")}
    auth_client.post("/api/attachment/upload", data={"conversation_id": cid}, files=files)
    auth_client.post("/api/chat/send",
                     json={"conversation_id": cid, "text": "2024-06 的销售额"})
    time.sleep(1.5)

    uid = auth_client.get("/auth/me").json()["id"]
    dirs = {k: config.STORAGE_DIR / k / str(uid) / str(cid)
            for k in ("uploads", "exports", "workspace")}

    auth_client.post("/api/chat/delete", json={"conversation_ids": [cid]})

    s = get_session()
    try:
        assert s.query(Conversation).filter_by(id=cid).one_or_none() is None
        assert s.query(Message).filter_by(conversation_id=cid).count() == 0
        assert s.query(Attachment).filter_by(conversation_id=cid).count() == 0
        assert s.query(AnalysisTask).filter_by(conversation_id=cid).count() == 0
        assert s.query(AnalysisResult).filter_by(conversation_id=cid).count() == 0
        assert s.query(ContextSummary).filter_by(conversation_id=cid).count() == 0
    finally:
        s.close()

    for kind, d in dirs.items():
        assert not d.exists(), f"{kind} 目录残留: {d}"


def test_ac15_delete_after_ws_token(auth_client):
    """签发过 WS 令牌的会话也能删——websocket_tokens 带外键，漏删会 FK 报错。"""
    from app.db import WebsocketToken, get_session

    cid = auth_client.post("/api/chat/create", json={"title": "带令牌"}).json()["conversation_id"]
    tok = auth_client.post("/api/chat/ws-token",
                           json={"conversation_id": cid}).json()
    assert tok.get("websocket_token")

    r = auth_client.post("/api/chat/delete", json={"conversation_ids": [cid]})
    assert r.status_code == 200, f"删除应成功，实际 {r.status_code}: {r.text[:200]}"
    assert cid in r.json()["deleted"]

    s = get_session()
    try:
        assert s.query(WebsocketToken).filter_by(conversation_id=cid).count() == 0, \
            "WS 令牌必须随会话删除"
    finally:
        s.close()


def test_ac15_delete_conversation_with_failed_attachment(auth_client):
    """附件解析失败会产生未绑定 task 的日志，也要一起删。"""
    from app.db import TaskLog, get_session

    cid = auth_client.post("/api/chat/create", json={"title": "失败附件"}).json()["conversation_id"]

    # 造一个未绑定 task 的日志（模拟解析失败的记录）
    s = get_session()
    try:
        s.add(TaskLog(task_id=None, conversation_id=cid, log_level="warning",
                      log_type="attachment", log_content="模拟解析失败"))
        s.commit()
    finally:
        s.close()

    r = auth_client.post("/api/chat/delete", json={"conversation_ids": [cid]})
    assert r.status_code == 200

    s = get_session()
    try:
        assert s.query(TaskLog).filter_by(conversation_id=cid).count() == 0
    finally:
        s.close()


# ------------------------------------------------------------------ AC-6 导出重下
def test_ac6_export_downloadable_twice(auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "导出"}).json()["conversation_id"]
    r = auth_client.post("/api/chat/send",
                         json={"conversation_id": cid,
                               "text": "2024-06 的销售额"}).json()
    tid = r["task_id"]
    for _ in range(60):
        st = auth_client.get(f"/api/tasks/{tid}").json()
        if st["task_status"] in ("success", "failed"):
            break
        time.sleep(0.3)

    a = auth_client.get(f"/api/results/{tid}/download")
    b = auth_client.get(f"/api/results/{tid}/download")
    assert a.status_code == b.status_code == 200
    assert a.text == b.text, "重复下载内容必须一致"


# ------------------------------------------------------------------ 计数器
def test_nfr2_rejection_counters_visible(admin_client, auth_client):
    cid = auth_client.post("/api/chat/create", json={"title": "计数"}).json()["conversation_id"]
    auth_client.post("/api/chat/send",
                     json={"conversation_id": cid, "text": "2024-06 的销售额"})
    auth_client.post("/api/chat/send",
                     json={"conversation_id": cid, "text": "并发"})

    rows = admin_client.get("/api/admin/rejections").json()
    codes = {r["code"]: r["count"] for r in rows}
    assert codes.get("task.concurrent_rejected", 0) >= 1, "拒绝必须有计数器"
