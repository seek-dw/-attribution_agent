"""初始化：建应用库 + 生成业务库示例数据 + 建演示账号。

用法: python scripts/init_db.py [--force]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.analysis import sample_data  # noqa: E402
from app.auth.session import hash_password  # noqa: E402
from app.db import (  # noqa: E402
    Base, User, create_all, get_session, init_engines,
)

DEMO_USERS = [
    ("analyst", "analyst123", "分析用户", "analyst"),
    ("admin", "admin123", "系统管理员", "admin"),
    ("ops", "ops123", "运营小王", "analyst"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="重建业务库与示例数据")
    args = ap.parse_args()

    config.ensure_dirs()
    init_engines()
    create_all()

    if args.force or not config.BIZ_DB.exists():
        print(f"生成业务库示例数据 → {config.BIZ_DB}")
        counts = sample_data.build(config.BIZ_DB)
        for k, v in sorted(counts.items()):
            print(f"  {k:22s} {v}")
    else:
        print(f"业务库已存在，跳过（用 --force 重建）：{config.BIZ_DB}")

    s = get_session()
    try:
        created = []
        for username, pw, display, role in DEMO_USERS:
            exists = s.query(User).filter_by(username=username).one_or_none()
            if exists:
                continue
            s.add(User(
                external_user_id=f"ext-{username}",
                username=username,
                display_name=display,
                password_hash=hash_password(pw),
                role=role,
            ))
            created.append(username)
        s.commit()
        print(f"演示账号：{created or '已存在，未重复创建'}")
        print("   analyst / analyst123   （分析用户）")
        print("   admin   / admin123     （管理员）")
        print("   ops     / ops123       （分析用户）")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
