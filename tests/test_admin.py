"""2.1.2 管理员五项职责：认证配置 / 系统配置 / 运行日志 / 数据源连接 / 功能开关。

这组测试是为补审计漏洞写的——原先的 audit_spec.py 只查了角色，
没查管理员职责清单，导致三项缺失被误算成"通过"。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

import pytest

# 沙箱下系统临时目录可能不可写，统一放到项目内 .pytest_data/tmp
TMP_ROOT = Path(__file__).resolve().parent.parent / ".pytest_data" / "ds_tmp"


@pytest.fixture()
def tmp_path():
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    d = TMP_ROOT / uuid.uuid4().hex[:10]
    d.mkdir(parents=True, exist_ok=True)
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ 认证配置
def test_admin_list_users(admin_client):
    r = admin_client.get("/api/admin/users")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 2
    assert any(u["username"] == "analyst" for u in rows)
    # 不得返回密码哈希
    for u in rows:
        assert "password_hash" not in u, "接口不得回显密码哈希"


def test_admin_create_user_and_login(admin_client, client):
    import uuid
    name = "u" + uuid.uuid4().hex[:8]
    r = admin_client.post("/api/admin/users", json={
        "username": name, "password": "test123456",
        "display_name": "测试用户", "role": "analyst",
    })
    assert r.status_code == 200, r.text
    uid = r.json()["id"]

    # 新用户能登录
    c2 = client
    r2 = c2.post("/auth/login", data={"username": name, "password": "test123456"},
                 follow_redirects=False)
    assert r2.status_code == 303, "新建用户应能登录"


def test_admin_create_user_rejects_duplicate(admin_client):
    r = admin_client.post("/api/admin/users", json={
        "username": "analyst", "password": "whatever123", "role": "analyst",
    })
    assert r.status_code == 409


def test_admin_create_user_rejects_short_password(admin_client):
    import uuid
    r = admin_client.post("/api/admin/users", json={
        "username": "p" + uuid.uuid4().hex[:8], "password": "123", "role": "analyst",
    })
    assert r.status_code == 400


def test_admin_reset_password(admin_client, client):
    import uuid
    name = "r" + uuid.uuid4().hex[:8]
    uid = admin_client.post("/api/admin/users", json={
        "username": name, "password": "old123456", "role": "analyst",
    }).json()["id"]

    r = admin_client.post(f"/api/admin/users/{uid}/password",
                          json={"password": "new123456"})
    assert r.status_code == 200

    # 旧密码失效、新密码可用
    c = client
    assert c.post("/auth/login", data={"username": name, "password": "old123456"},
                  follow_redirects=False).status_code == 401
    assert c.post("/auth/login", data={"username": name, "password": "new123456"},
                  follow_redirects=False).status_code == 303


def test_admin_cannot_self_demote(admin_client):
    me = admin_client.get("/auth/me").json()
    r = admin_client.post(f"/api/admin/users/{me['id']}/role",
                          json={"role": "analyst"})
    assert r.status_code == 400, "不能把自己降级，否则锁死管理员入口"


def test_admin_cannot_disable_self(admin_client):
    me = admin_client.get("/auth/me").json()
    r = admin_client.post(f"/api/admin/users/{me['id']}/status",
                          json={"status": "disabled"})
    assert r.status_code == 400


def test_disabled_user_cannot_login(admin_client, client):
    import uuid
    name = "d" + uuid.uuid4().hex[:8]
    uid = admin_client.post("/api/admin/users", json={
        "username": name, "password": "test123456", "role": "analyst",
    }).json()["id"]
    admin_client.post(f"/api/admin/users/{uid}/status", json={"status": "disabled"})

    r = client.post("/auth/login", data={"username": name, "password": "test123456"},
                    follow_redirects=False)
    assert r.status_code == 401, "停用用户不得登录"


def test_analyst_cannot_access_user_admin(auth_client):
    assert auth_client.get("/api/admin/users").status_code == 403
    assert auth_client.post("/api/admin/users", json={
        "username": "x", "password": "xxxxxx", "role": "analyst",
    }).status_code == 403


# ------------------------------------------------------------------ 功能开关
def test_switches_present_in_config(admin_client):
    cfg = admin_client.get("/api/admin/config").json()
    for k in ("enable_attachment", "enable_command_tool", "enable_file_tools",
              "enable_text_search", "enable_context_summary", "enable_export"):
        assert k in cfg, f"缺少功能开关 {k}"
        assert isinstance(cfg[k], bool), f"{k} 应为布尔值"


def test_disable_attachment_blocks_upload(admin_client, auth_client):
    import io
    admin_client.post("/api/admin/reload",
                      json={"updates": {"enable_attachment": False}})
    try:
        cid = auth_client.post("/api/chat/create",
                               json={"title": "开关测试"}).json()["conversation_id"]
        files = {"file": ("a.txt", io.BytesIO(b"x"), "text/plain")}
        r = auth_client.post("/api/attachment/upload",
                             data={"conversation_id": cid}, files=files)
        assert r.status_code == 403, "关闭附件后上传应被拒"
    finally:
        admin_client.post("/api/admin/reload",
                          json={"updates": {"enable_attachment": True}})


def test_disable_export_blocks_download(admin_client, auth_client):
    cid = auth_client.post("/api/chat/create",
                           json={"title": "导出开关"}).json()["conversation_id"]
    r = auth_client.post("/api/chat/send", json={
        "conversation_id": cid, "text": "2024-06 的销售额"})
    tid = r.json()["task_id"]
    for _ in range(80):
        st = auth_client.get(f"/api/tasks/{tid}").json()
        if st["task_status"] in ("success", "failed"):
            break
        time.sleep(0.3)

    admin_client.post("/api/admin/reload", json={"updates": {"enable_export": False}})
    try:
        d = auth_client.get(f"/api/results/{tid}/download")
        assert d.status_code == 403, "关闭导出后下载应被拒"
    finally:
        admin_client.post("/api/admin/reload",
                          json={"updates": {"enable_export": True}})


def test_switch_off_recorded_in_result_note(admin_client, auth_client):
    """关闭命令执行后，结果里要说明本次没用该能力（不静默）。"""
    admin_client.post("/api/admin/reload",
                      json={"updates": {"enable_command_tool": False}})
    try:
        cid = auth_client.post("/api/chat/create",
                               json={"title": "开关备注"}).json()["conversation_id"]
        r = auth_client.post("/api/chat/send", json={
            "conversation_id": cid,
            "text": "为什么 2024-06 的销售额下滑了？帮我算一下口径对不对",
        })
        tid = r.json()["task_id"]
        for _ in range(80):
            st = auth_client.get(f"/api/tasks/{tid}").json()
            if st["task_status"] in ("success", "failed"):
                break
            time.sleep(0.3)

        tools = auth_client.get(f"/api/tasks/{tid}/tools").json()
        assert not any(t["tool_name"] == "command_run" for t in tools), \
            "关闭后不应调用命令执行"

        res = auth_client.get(f"/api/results/{tid}").json()
        assert "命令执行" in (res.get("degraded_note") or ""), \
            "关闭的能力必须在结果中说明，不能静默"
    finally:
        admin_client.post("/api/admin/reload",
                          json={"updates": {"enable_command_tool": True}})


# ------------------------------------------------------------------ 数据源
def _make_db(path, tables_spec):
    con = sqlite3.connect(str(path))
    try:
        for ddl in tables_spec:
            con.execute(ddl)
        con.commit()
    finally:
        con.close()


def test_data_source_list_empty(admin_client):
    r = admin_client.get("/api/admin/data-sources")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_data_source_register_and_inspect(admin_client, tmp_path):
    db = tmp_path / "biz.db"
    _make_db(db, [
        "CREATE TABLE orders (order_id INT, product_id INT, order_date TEXT, "
        "amount REAL, status TEXT)",
        "CREATE TABLE products (product_id INT, category_id INT, price REAL)",
        "CREATE TABLE categories (category_id INT, category_name TEXT)",
        "CREATE TABLE traffic_daily (stat_date TEXT, visitors INT)",
        "CREATE TABLE refunds (refund_id INT, order_id INT, refund_date TEXT, amount REAL)",
        "CREATE TABLE reviews (review_id INT, review_date TEXT, rating INT)",
        "CREATE TABLE inventory (snapshot_date TEXT, stock_qty INT, warning_threshold INT)",
        "CREATE TABLE competitor_prices (snapshot_date TEXT, price REAL)",
    ])

    r = admin_client.post("/api/admin/data-sources", json={
        "name": "完整库", "path": str(db), "read_only": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    ds_id = r.json()["id"]

    ins = admin_client.get(f"/api/admin/data-sources/{ds_id}/inspect").json()
    assert ins["ready"] is True
    assert ins["coverage"] == 100.0
    assert not ins["missing_required"]

    admin_client.delete(f"/api/admin/data-sources/{ds_id}")


def test_data_source_incomplete_reports_missing(admin_client, tmp_path):
    db = tmp_path / "partial.db"
    _make_db(db, [
        "CREATE TABLE orders (order_id INT, product_id INT, order_date TEXT, "
        "amount REAL, status TEXT)",
    ])

    ds_id = admin_client.post("/api/admin/data-sources", json={
        "name": "残缺库", "path": str(db),
    }).json()["id"]

    ins = admin_client.get(f"/api/admin/data-sources/{ds_id}/inspect").json()
    assert ins["ready"] is False
    assert "products" in ins["missing_required"]
    assert ins["impact"], "必须说明缺表对分析的影响"
    assert any("证据不足" in x for x in ins["impact"])

    admin_client.delete(f"/api/admin/data-sources/{ds_id}")


def test_missing_optional_tables_have_distinct_effects(admin_client, tmp_path):
    """可选表缺失时，每条影响说明要具体，不能是同一句话复读。"""
    db = tmp_path / "opt.db"
    _make_db(db, [
        "CREATE TABLE orders (order_id INT, product_id INT, order_date TEXT, "
        "amount REAL, status TEXT)",
        "CREATE TABLE products (product_id INT, category_id INT, price REAL)",
        "CREATE TABLE categories (category_id INT, category_name TEXT)",
        "CREATE TABLE traffic_daily (stat_date TEXT, visitors INT)",
        "CREATE TABLE refunds (refund_id INT, order_id INT, refund_date TEXT, amount REAL)",
        "CREATE TABLE reviews (review_id INT, review_date TEXT, rating INT)",
        "CREATE TABLE inventory (snapshot_date TEXT, stock_qty INT, warning_threshold INT)",
        "CREATE TABLE competitor_prices (snapshot_date TEXT, price REAL)",
    ])
    ds_id = admin_client.post("/api/admin/data-sources", json={
        "name": "无可选表库", "path": str(db),
    }).json()["id"]

    ins = admin_client.get(f"/api/admin/data-sources/{ds_id}/inspect").json()
    assert ins["ready"] is True, "必需表齐全就算可支撑分析"
    opt_impacts = [x for x in ins["impact"] if any(
        t in x for t in ("traffic_channel", "promotion_daily", "refund_reasons",
                         "search_exposure", "comments"))]
    assert opt_impacts, "应列出可选表影响"
    # 每条说明互不相同
    assert len(set(opt_impacts)) == len(opt_impacts), \
        f"影响说明不应重复：{opt_impacts}"

    admin_client.delete(f"/api/admin/data-sources/{ds_id}")


def test_data_source_nonexistent_path(admin_client, tmp_path):
    r = admin_client.post("/api/admin/data-sources", json={
        "name": "不存在", "path": str(tmp_path / "nope.db"),
    })
    # 登记成功但状态为 error（不静默丢弃，便于排查）
    assert r.status_code == 200
    assert r.json()["status"] == "error"
    admin_client.delete(f"/api/admin/data-sources/{r.json()['id']}")


def test_data_source_duplicate_name_rejected(admin_client, tmp_path):
    db = tmp_path / "dup.db"
    _make_db(db, ["CREATE TABLE t (a INT)"])
    first = admin_client.post("/api/admin/data-sources", json={
        "name": "重名库", "path": str(db),
    }).json()
    r = admin_client.post("/api/admin/data-sources", json={
        "name": "重名库", "path": str(db),
    })
    assert r.status_code == 409
    admin_client.delete(f"/api/admin/data-sources/{first['id']}")


def test_activate_incomplete_requires_force(admin_client, tmp_path):
    db = tmp_path / "partial2.db"
    _make_db(db, ["CREATE TABLE orders (order_id INT)"])
    ds_id = admin_client.post("/api/admin/data-sources", json={
        "name": "待切换残缺库", "path": str(db),
    }).json()["id"]

    r = admin_client.post(f"/api/admin/data-sources/{ds_id}/activate", json={})
    assert r.status_code == 400
    assert "force" in r.text

    # 强制切换可以，但要带告警
    r2 = admin_client.post(f"/api/admin/data-sources/{ds_id}/activate",
                           json={"force": True})
    assert r2.status_code == 200
    assert r2.json()["warnings"]

    # 切回默认（清空 active）
    admin_client.post(f"/api/admin/data-sources/{ds_id}/activate",
                      json={"force": True})
    admin_client.delete(f"/api/admin/data-sources/{ds_id}")


def test_activate_complete_source_and_analyze(admin_client, auth_client, tmp_path):
    """切换到完整库后，分析应真实读新库（不是还读旧库）。"""
    db = tmp_path / "switch.db"
    con = sqlite3.connect(str(db))
    try:
        con.executescript("""
        CREATE TABLE categories (category_id INT, category_name TEXT);
        CREATE TABLE products (product_id INT, category_id INT, price REAL,
                               product_name TEXT, cost REAL);
        CREATE TABLE orders (order_id INT, product_id INT, order_date TEXT,
                             amount REAL, status TEXT);
        CREATE TABLE traffic_daily (stat_date TEXT, visitors INT);
        CREATE TABLE refunds (refund_id INT, order_id INT, refund_date TEXT,
                              amount REAL, reason_id INT);
        CREATE TABLE reviews (review_id INT, product_id INT, review_date TEXT, rating INT);
        CREATE TABLE inventory (snapshot_date TEXT, sku_id INT, product_id INT,
                                stock_qty INT, warning_threshold INT);
        CREATE TABLE competitor_prices (snapshot_date TEXT, competitor_name TEXT,
                                        category_id INT, price REAL);
        INSERT INTO categories VALUES (1, '测试类目');
        INSERT INTO products VALUES (1, 1, 100, '测试商品', 60);
        INSERT INTO orders VALUES (1, 1, '2024-06-15', 1000, 'paid');
        INSERT INTO orders VALUES (2, 1, '2024-06-16', 2000, 'paid');
        INSERT INTO traffic_daily VALUES ('2024-06-15', 100);
        INSERT INTO traffic_daily VALUES ('2024-06-16', 120);
        """)
        con.commit()
    finally:
        con.close()

    ds_id = admin_client.post("/api/admin/data-sources", json={
        "name": "切换测试库", "path": str(db),
    }).json()["id"]
    r = admin_client.post(f"/api/admin/data-sources/{ds_id}/activate", json={})
    assert r.status_code == 200, r.text

    try:
        cid = auth_client.post("/api/chat/create",
                               json={"title": "切库后分析"}).json()["conversation_id"]
        rr = auth_client.post("/api/chat/send", json={
            "conversation_id": cid, "text": "2024-06 的销售额",
        })
        tid = rr.json()["task_id"]
        for _ in range(80):
            st = auth_client.get(f"/api/tasks/{tid}").json()
            if st["task_status"] in ("success", "failed"):
                break
            time.sleep(0.3)

        res = auth_client.get(f"/api/results/{tid}").json()
        # 新库只有 3000 元，若还读旧库会是百万级
        vals = [m["metric_value"] for m in res["key_metrics"]
                if m["metric_name"] == "销售额"]
        assert vals, f"应有销售额指标：{res}"
        assert vals[0] == pytest.approx(3000.0, abs=1.0), \
            f"应读新库的 3000 元，实际 {vals[0]}"
    finally:
        admin_client.delete(f"/api/admin/data-sources/{ds_id}")


def test_analyst_cannot_manage_data_sources(auth_client):
    assert auth_client.get("/api/admin/data-sources").status_code == 403
    assert auth_client.post("/api/admin/data-sources",
                            json={"name": "x", "path": "/tmp/x.db"}).status_code == 403


def test_first_source_auto_activated(admin_client, tmp_path):
    """第一个登记的数据源自动激活，否则"加了但没生效"。"""
    db = tmp_path / "auto.db"
    _make_db(db, ["CREATE TABLE t (a INT)"])
    before = admin_client.get("/api/admin/data-sources").json()
    no_active_before = not any(d["is_active"] for d in before)

    ds_id = admin_client.post("/api/admin/data-sources", json={
        "name": "自动激活库", "path": str(db),
    }).json()["id"]

    rows = admin_client.get("/api/admin/data-sources").json()
    row = next(d for d in rows if d["id"] == ds_id)

    if no_active_before and len(before) == 0:
        assert row["is_active"] is True, "首个数据源应自动激活"

    admin_client.delete(f"/api/admin/data-sources/{ds_id}")


def test_can_delete_active_source_no_deadlock(admin_client, tmp_path):
    """激活中的数据源必须可删。

    原实现要求"先切到别处才能删"，但只有一个数据源时不存在"别处"，
    会形成死锁：删不掉、也切不走。
    """
    db = tmp_path / "locked.db"
    _make_db(db, ["CREATE TABLE orders (order_id INT, product_id INT, "
                  "order_date TEXT, amount REAL, status TEXT)"])
    ds_id = admin_client.post("/api/admin/data-sources", json={
        "name": "要删的激活库", "path": str(db), "read_only": True,
    }).json()["id"]
    admin_client.post(f"/api/admin/data-sources/{ds_id}/activate",
                      json={"force": True})

    rows = admin_client.get("/api/admin/data-sources").json()
    assert any(d["id"] == ds_id and d["is_active"] for d in rows), "前置：应为激活状态"

    r = admin_client.delete(f"/api/admin/data-sources/{ds_id}")
    assert r.status_code == 200, f"激活中的数据源应可删，实际 {r.status_code}: {r.text[:150]}"

    body = r.json()
    assert body["was_active"] is True
    assert body["fallback"], "删除后必须说明回落到哪里"

    rows2 = admin_client.get("/api/admin/data-sources").json()
    assert not any(d["id"] == ds_id for d in rows2), "删除后不应再出现在列表"


def test_delete_active_falls_back_to_builtin_db(admin_client, auth_client, tmp_path):
    """删掉唯一的数据源后，分析要回落到内置示例库并正常工作。"""
    db = tmp_path / "tiny.db"
    con = sqlite3.connect(str(db))
    try:
        con.executescript("""
        CREATE TABLE categories (category_id INT, category_name TEXT);
        CREATE TABLE products (product_id INT, category_id INT, price REAL,
                               product_name TEXT, cost REAL);
        CREATE TABLE orders (order_id INT, product_id INT, order_date TEXT,
                             amount REAL, status TEXT);
        CREATE TABLE traffic_daily (stat_date TEXT, visitors INT);
        CREATE TABLE refunds (refund_id INT, order_id INT, refund_date TEXT,
                              amount REAL, reason_id INT);
        CREATE TABLE reviews (review_id INT, product_id INT, review_date TEXT, rating INT);
        CREATE TABLE inventory (snapshot_date TEXT, sku_id INT, product_id INT,
                                stock_qty INT, warning_threshold INT);
        CREATE TABLE competitor_prices (snapshot_date TEXT, competitor_name TEXT,
                                        category_id INT, price REAL);
        INSERT INTO categories VALUES (1, '小库类目');
        INSERT INTO products VALUES (1, 1, 100, '小库商品', 60);
        INSERT INTO orders VALUES (1, 1, '2024-06-01', 777, 'paid');
        """)
        con.commit()
    finally:
        con.close()

    ds_id = admin_client.post("/api/admin/data-sources", json={
        "name": "临时小库", "path": str(db),
    }).json()["id"]
    admin_client.post(f"/api/admin/data-sources/{ds_id}/activate", json={"force": True})

    # 删掉它
    r = admin_client.delete(f"/api/admin/data-sources/{ds_id}")
    assert r.status_code == 200
    assert "内置示例库" in r.json()["message"]

    # 回落后再分析，应读到示例库（百万级），不是 777
    cid = auth_client.post("/api/chat/create",
                           json={"title": "回落验证"}).json()["conversation_id"]
    rr = auth_client.post("/api/chat/send", json={
        "conversation_id": cid, "text": "2024-06 的销售额"})
    tid = rr.json()["task_id"]
    for _ in range(80):
        st = auth_client.get(f"/api/tasks/{tid}").json()
        if st["task_status"] in ("success", "failed"):
            break
        time.sleep(0.3)

    res = auth_client.get(f"/api/results/{tid}").json()
    vals = [m["metric_value"] for m in res.get("key_metrics") or []
            if m["metric_name"] == "销售额"]
    assert vals, f"应有销售额：{res}"
    assert vals[0] > 100000, f"应回落读示例库（百万级），实际 {vals[0]}"


# ------------------------------------------------------------------ 运行日志
def test_admin_logs_filterable(admin_client, auth_client):
    cid = auth_client.post("/api/chat/create",
                           json={"title": "日志筛选"}).json()["conversation_id"]
    auth_client.post("/api/chat/send", json={
        "conversation_id": cid, "text": "2024-06 的销售额"})
    time.sleep(2)

    all_logs = admin_client.get("/api/admin/logs?limit=100").json()
    assert all_logs

    only_info = admin_client.get("/api/admin/logs?level=info&limit=100").json()
    assert all(r["log_level"] == "info" for r in only_info)
    assert all("password" not in r["log_content"].lower() for r in all_logs), \
        "日志不得含凭据明文"
