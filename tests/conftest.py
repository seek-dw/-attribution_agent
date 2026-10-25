"""测试夹具：独立临时环境，不污染开发数据。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _env():
    """把 config 的数据目录指向项目内临时目录，避免动到真实 data/。"""
    import shutil

    from app import config

    tmp = ROOT / ".pytest_data"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    config.DATA_DIR = tmp
    config.APP_DB = tmp / "app.db"
    config.BIZ_DB = tmp / "business.db"
    config.STORAGE_DIR = tmp / "storage"
    config.CONFIG_FILE = tmp / "runtime_config.json"
    config.ensure_dirs()
    yield
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def biz():
    from app import config
    from app.analysis import sample_data

    if not config.BIZ_DB.exists():
        sample_data.build(config.BIZ_DB)
    return config.BIZ_DB


@pytest.fixture(scope="session", autouse=True)
def db(_env, biz):
    from app.db import create_all, init_engines

    init_engines()
    create_all()
    # 建演示账号
    from app.auth.session import hash_password
    from app.db import User, get_session

    s = get_session()
    try:
        for username, pw, role in (("analyst", "analyst123", "analyst"),
                                   ("admin", "admin123", "admin"),
                                   ("other", "other123", "analyst")):
            if s.query(User).filter_by(username=username).one_or_none() is None:
                s.add(User(external_user_id=f"ext-{username}", username=username,
                           display_name=username, password_hash=hash_password(pw),
                           role=role))
        s.commit()
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clean_data_sources():
    """每个用例前清空数据源登记。

    原因：biz_engine() 会读 data_sources 表决定用哪个库。
    若某个用例切换到测试库后没清干净，后续用例会读到错的库，
    表现为"单独跑能过、整批跑就失败"的相互污染。
    """
    from app.db import DataSource, get_session

    s = get_session()
    try:
        s.query(DataSource).delete(synchronize_session=False)
        s.commit()
    except Exception:  # noqa: BLE001
        s.rollback()
    finally:
        s.close()
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_client():
    """独立实例：避免与 admin_client 共用登录态互相覆盖。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post("/auth/login",
                   data={"username": "analyst", "password": "analyst123"},
                   follow_redirects=False)
        assert r.status_code == 303
        yield c


@pytest.fixture()
def admin_client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post("/auth/login",
                   data={"username": "admin", "password": "admin123"},
                   follow_redirects=False)
        assert r.status_code == 303
        yield c
