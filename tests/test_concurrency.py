"""并发与连接管理：防止连接池耗尽导致服务不可用。

对应缺陷：/workbench 曾因 get_session 未释放 + 池过小，
返回 500 QueuePool limit of size 5 reached。
"""

from __future__ import annotations

import concurrent.futures
import io
import time

import pytest


def test_concurrent_page_requests_do_not_exhaust_pool(auth_client):
    """并发访问页面 + 接口，不得出现连接池耗尽。"""
    def hit(i):
        if i % 3 == 0:
            return auth_client.get("/workbench").status_code
        if i % 3 == 1:
            return auth_client.get("/api/chat/ls").status_code
        return auth_client.get("/api/admin/rejections").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        codes = list(ex.map(hit, range(36)))

    bad = [c for c in codes if c >= 500]
    assert not bad, f"并发下出现 5xx：{bad}"


def test_workbench_accessible_for_both_roles(auth_client, admin_client):
    assert auth_client.get("/workbench").status_code == 200
    assert admin_client.get("/workbench").status_code == 200


def test_concurrent_analysis_and_page_load(auth_client):
    """分析任务运行期间，页面与列表接口仍可用（不因 Session 占用阻塞）。"""
    cid = auth_client.post("/api/chat/create", json={"title": "并发分析"}).json()["conversation_id"]
    auth_client.post("/api/chat/send", json={
        "conversation_id": cid, "text": "为什么 2024-06 的销售额下滑了？",
    })

    codes = []
    for _ in range(10):
        codes.append(auth_client.get("/workbench").status_code)
        codes.append(auth_client.get("/api/chat/ls").status_code)
        time.sleep(0.2)

    assert all(c == 200 for c in codes), f"分析期间请求失败：{codes}"


def test_session_released_after_request(auth_client):
    """连续大量请求后连接池仍有余量（会话被归还）。"""
    for _ in range(60):
        r = auth_client.get("/api/chat/ls")
        assert r.status_code == 200

    # 仍能正常服务
    assert auth_client.get("/workbench").status_code == 200
