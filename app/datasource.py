"""数据源管理（2.1.2：管理员管理数据源连接）。

职责：
- 登记/删除/启用数据源
- 连通性测试与表结构探查
- 检查是否满足分析所需的最小表集，并给出缺什么

设计原则：
- 不存凭据明文（SQLite 只需路径）
- 分析只在只读连接上跑
- 缺失的表要明确报出来，不静默让分析跑空
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

# 分析引擎依赖的表与关键字段。缺任一表，相关假设就无法取证。
REQUIRED_SCHEMA: dict[str, tuple[str, ...]] = {
    "orders": ("order_id", "product_id", "order_date", "amount", "status"),
    "products": ("product_id", "category_id", "price"),
    "categories": ("category_id", "category_name"),
    "traffic_daily": ("stat_date", "visitors"),
    "refunds": ("refund_id", "order_id", "refund_date", "amount"),
    "reviews": ("review_id", "review_date", "rating"),
    "inventory": ("snapshot_date", "stock_qty", "warning_threshold"),
    "competitor_prices": ("snapshot_date", "price"),
}

# 有这些表能让对应假设可验证；缺了只是该假设会判"证据不足"
OPTIONAL_SCHEMA: dict[str, tuple[str, ...]] = {
    "traffic_channel": ("stat_date", "channel_id", "visitors"),
    "promotion_daily": ("stat_date", "promo_type", "discount_pct"),
    "refund_reasons": ("reason_id", "reason_name"),
    "search_exposure": ("stat_date", "product_id", "exposure", "clicks"),
    "comments": ("comment_id", "content"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def inspect_sqlite(path: str) -> dict[str, Any]:
    """只读探查一个 SQLite 文件：能否打开、有哪些表、字段是否齐。

    返回结构直接给前端用，不抛异常——所有失败都变成可读结论。
    """
    p = Path(path)
    result: dict[str, Any] = {
        "path": str(p),
        "exists": p.exists(),
        "tables": [],
        "missing_required": [],
        "missing_optional": [],
        "field_issues": {},
        "coverage": 0.0,
        "ready": False,
        "error": "",
    }

    if not p.exists():
        result["error"] = f"文件不存在：{p}"
        return result
    if not p.is_file():
        result["error"] = f"不是文件：{p}"
        return result

    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        result["error"] = f"无法以只读方式打开：{exc}"
        return result

    try:
        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]
        result["tables"] = tables

        columns: dict[str, set[str]] = {}
        for t in tables:
            try:
                c2 = con.execute(f'PRAGMA table_info("{t}")')
                columns[t] = {row[1] for row in c2.fetchall()}
            except sqlite3.Error:
                columns[t] = set()

        # 必需表的检查
        for t, need in REQUIRED_SCHEMA.items():
            if t not in tables:
                result["missing_required"].append(t)
                continue
            lack = [c for c in need if c not in columns.get(t, set())]
            if lack:
                result["field_issues"][t] = lack

        for t, need in OPTIONAL_SCHEMA.items():
            if t not in tables:
                result["missing_optional"].append(t)
                continue
            lack = [c for c in need if c not in columns.get(t, set())]
            if lack:
                result["field_issues"][t] = lack

        need_n = len(REQUIRED_SCHEMA)
        ok_n = sum(
            1 for t in REQUIRED_SCHEMA
            if t in tables and not result["field_issues"].get(t)
        )
        result["coverage"] = round(ok_n / need_n * 100, 1)
        result["ready"] = (not result["missing_required"]
                           and not any(t in REQUIRED_SCHEMA
                                       for t in result["field_issues"]))
    except sqlite3.Error as exc:
        result["error"] = f"探查失败：{exc}"
    finally:
        con.close()

    result["impact"] = build_impact(result)
    return result


# 可选表缺失时，具体影响哪类分析——说清后果，而不是复读"不影响主流程"
OPTIONAL_EFFECTS: dict[str, str] = {
    "traffic_channel": "无法判断是否由某个渠道流量变化导致",
    "promotion_daily": "无法判断促销力度变化的影响",
    "refund_reasons": "无法按退款原因细分，只能看退款总量",
    "search_exposure": "无法计算曝光到点击的漏斗损失",
    "comments": "无法从评论文本中提取具体不满点",
}


def build_impact(info: dict[str, Any]) -> list[str]:
    """把缺表/缺字段翻译成"哪些分析会退化"，供接口与前端直接展示。"""
    impact: list[str] = []
    for t in info.get("missing_required", []):
        impact.append(f"缺表 {t}：相关归因假设将判「证据不足」")
    for t, cols in (info.get("field_issues") or {}).items():
        impact.append(f"表 {t} 缺字段 {', '.join(cols)}：对应指标无法计算")
    for t in info.get("missing_optional", []):
        effect = OPTIONAL_EFFECTS.get(t, "相关维度不做推断")
        impact.append(f"缺表 {t}：{effect}")
    return impact


def test_connection(path: str, read_only: bool = True) -> tuple[bool, str]:
    """连通性测试：能建立连接并执行一次极轻查询。"""
    p = Path(path)
    if not p.exists():
        return False, f"文件不存在：{p}"
    try:
        if read_only:
            con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
        else:
            con = sqlite3.connect(str(p), timeout=5)
        try:
            con.execute("SELECT 1").fetchone()
        finally:
            con.close()
        return True, "连接正常"
    except sqlite3.Error as exc:
        return False, f"连接失败：{exc}"


def sample_rows(engine, table: str, limit: int = 3) -> list[dict[str, Any]]:
    """取几行样本给管理员看，帮助确认字段含义。"""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f'SELECT * FROM "{table}" LIMIT :n'),
                                {"n": limit}).fetchall()
            cols = list(rows[0]._mapping.keys()) if rows else []
            return [dict(zip(cols, r)) for r in rows]
    except Exception:  # noqa: BLE001
        return []
