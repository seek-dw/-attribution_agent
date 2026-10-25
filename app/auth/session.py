"""认证：本地认证服务替代真实认证中心（范围外声明见 SPEC §4.2）。

保留"入口页 → 回调换令牌 → 建立登录态"的形态，便于将来替换真实认证中心。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .. import config
from ..db import User, session_dep

COOKIE_NAME = "attr_session"
SESSION_TTL = timedelta(hours=12)


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(password, salt), stored)


def _sign(payload: str) -> str:
    mac = hmac.new(config.secret_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{mac}"


def issue_session(user_id: int) -> str:
    exp = int((datetime.now(timezone.utc) + SESSION_TTL).timestamp())
    return _sign(f"{user_id}:{exp}")


def parse_session(token: str | None) -> int | None:
    if not token or "." not in token:
        return None
    payload, mac = token.rsplit(".", 1)
    expected = hmac.new(config.secret_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        uid_s, exp_s = payload.split(":", 1)
        if int(exp_s) < int(datetime.now(timezone.utc).timestamp()):
            return None
        return int(uid_s)
    except ValueError:
        return None


def current_user(request: Request, s: Session = Depends(session_dep)) -> User:
    """FR-1.5：所有业务接口必须做登录态校验。"""
    uid = parse_session(request.cookies.get(COOKIE_NAME))
    if uid is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或登录态已失效")
    user = s.get(User, uid)
    if user is None or user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已停用")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    """FR-1.4：管理员专属能力不对分析用户开放。"""
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员角色")
    return user
