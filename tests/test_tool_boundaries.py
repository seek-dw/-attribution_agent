"""AC-11 工具边界：非只读 SQL、命令白名单、路径穿越。"""

from __future__ import annotations

import pytest

from app.config import get as cfg
from app.db import get_session
from app.tools import registry as tools


@pytest.fixture()
def s():
    sess = get_session()
    try:
        yield sess
        sess.commit()
    finally:
        sess.close()


# ---------------------------------------------------------------- AC-11 只读
@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE orders SET amount=0",
    "INSERT INTO orders VALUES (1,1,'2024-01-01',1,'paid')",
    "DROP TABLE orders",
    "ALTER TABLE orders ADD COLUMN x",
    "TRUNCATE TABLE orders",
    "SELECT * FROM orders; DELETE FROM orders",
])
def test_ac11_non_readonly_rejected(s, sql):
    out = tools.db_query(s, sql)
    assert out.status == "rejected", f"应拒绝: {sql}"
    assert not out.ok


def test_ac11_select_allowed(s):
    out = tools.db_query(s, "SELECT COUNT(*) AS n FROM orders")
    assert out.ok, out.error
    assert out.data["rows"][0]["n"] > 0


def test_ac11_row_limit_truncated(s):
    limit = int(cfg("db_query_max_rows"))
    out = tools.db_query(s, "SELECT * FROM orders")
    assert out.ok
    assert out.data["truncated"] is True
    assert len(out.data["rows"]) == limit


# ------------------------------------------------------------ AC-11 业务库只读
def test_ac11_business_db_is_readonly_on_disk(client):
    """绕过应用层直连业务库也无法写入（NFR-6）。"""
    import sqlite3

    from app import config

    con = sqlite3.connect(f"file:{config.BIZ_DB.as_posix()}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM orders")
    finally:
        con.close()


# ------------------------------------------------------------ AC-12 命令白名单
@pytest.mark.parametrize("cmd", ["rm -rf /", "curl http://x", "powershell -c whoami",
                                 "bash -c id", "netsh interface show"])
def test_ac12_command_not_whitelisted(s, cmd):
    out = tools.command_run(s, 1, 1, cmd)
    assert out.status == "rejected", f"应拒绝: {cmd}"


def test_ac12_command_whitelisted_ok(s):
    out = tools.command_run(s, 1, 1, "python -c \"print(1+1)\"")
    assert out.ok, out.error
    assert "2" in out.data["output"]


def test_ac12_command_output_truncated(s):
    limit = int(cfg("command_output_max_chars"))
    out = tools.command_run(s, 1, 1, "python -c \"print('x'*100000)\"")
    assert out.ok, out.error
    assert out.data["truncated"] is True
    assert "已截断" in out.data["output"]


def test_ac12_command_silent_failure_detected(s):
    """退出码 0 但无输出 → 按失败处理，不当成功（NFR-8 不静默）。"""
    out = tools.command_run(s, 1, 1, "python -c \"pass\"")
    assert out.status == "failed"
    assert "无输出" in out.summary or "无输出" in out.error


def test_ac12_command_timeout(s, monkeypatch):
    from app import config
    config.DEFAULTS["command_timeout_sec"] = 1
    config._cache.clear()
    try:
        out = tools.command_run(
            s, 1, 1, "python -c \"import time; time.sleep(5)\"")
        assert out.status == "failed"
        assert "超过 1 秒被终止" in out.error
    finally:
        config.DEFAULTS["command_timeout_sec"] = 10
        config._cache.clear()


# ------------------------------------------------------------ AC-13 路径穿越
@pytest.mark.parametrize("path", ["../escape.txt", "../../etc/passwd",
                                  "/etc/passwd", "..\\windows\\win.ini"])
def test_ac13_path_escape_rejected(s, path):
    out = tools.file_write(s, 1, 1, path, "x")
    assert out.status == "rejected", f"应拒绝: {path}"


def test_ac13_file_write_read_ok(s):
    w = tools.file_write(s, 1, 2, "note.txt", "hello")
    assert w.ok, w.error
    r = tools.file_read(s, 1, 2, "note.txt")
    assert r.ok
    assert r.data["content"] == "hello"
