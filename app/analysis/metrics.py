"""指标定义与周期解析。

所有指标都有明确口径（SPEC §3「口径」），可复现（FR-6.2）：
每个指标 = 一条确定的 SQL + 明确的过滤条件。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class Period:
    start: date
    end: date          # 含
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "label": self.label, "days": self.days}


def _month_range(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _shift_month(d: date, delta: int) -> tuple[int, int]:
    idx = (d.year * 12 + d.month - 1) + delta
    return idx // 12, idx % 12 + 1


def parse_period(text_: str, today: date | None = None) -> Period | None:
    """把自然语言周期解析为区间；无法确定时返回 None（触发澄清 FR-5.6）。"""
    today = today or date.today()
    t = (text_ or "").strip()

    m = re.search(r"(\d{4})-(\d{1,2})", t)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        s, e = _month_range(y, mo)
        return Period(s, e, f"{y}年{mo}月")

    if re.search(r"上上个月|上上月", t):
        y, mo = _shift_month(today, -2)
        s, e = _month_range(y, mo)
        return Period(s, e, f"{mo}月（上上月）")

    if re.search(r"上个?月|上月", t):
        y, mo = _shift_month(today, -1)
        s, e = _month_range(y, mo)
        return Period(s, e, f"{mo}月（上月）")

    if re.search(r"本月|这个月|当月", t):
        s, e = _month_range(today.year, today.month)
        return Period(s, min(e, today), f"{today.month}月（本月至今）")

    m = re.search(r"最近\s*(\d+)\s*天", t)
    if m:
        n = max(1, int(m.group(1)))
        return Period(today - timedelta(days=n - 1), today, f"最近{n}天")

    m = re.search(r"近\s*(\d+)\s*周", t)
    if m:
        n = max(1, int(m.group(1)))
        return Period(today - timedelta(days=n * 7 - 1), today, f"近{n}周")

    m = re.search(r"(\d{4})年(\d{1,2})月", t)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        s, e = _month_range(y, mo)
        return Period(s, e, f"{y}年{mo}月")

    return None


def previous_equal_period(p: Period) -> Period:
    """取紧邻的等长区间作为基线。

    若本区间恰好是完整自然月，基线取上一个完整自然月（口径对齐，可比）；
    否则按等长前推。
    """
    is_full_month = (p.start.day == 1
                     and p.end.day == calendar.monthrange(p.start.year,
                                                          p.start.month)[1]
                     and p.start.year == p.end.year and p.start.month == p.end.month)
    if is_full_month:
        y, mo = _shift_month(p.start, -1)
        s, e = _month_range(y, mo)
        return Period(s, e, f"{y}年{mo}月（基线）")

    span = timedelta(days=p.days)
    end = p.start - timedelta(days=1)
    start = end - span + timedelta(days=1)
    return Period(start, end, f"基线（{start.isoformat()}~{end.isoformat()}）")


# --------------------------------------------------------------------------
# 指标口径表：每个指标一条 SQL + 单位 + 口径说明
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MetricDef:
    key: str
    label: str
    unit: str
    definition: str          # 口径说明，进入结果供核验
    sql: str
    scale: float = 1.0       # 口径 SQL 返回值到展示单位的换算（比率→百分比）

    @property
    def display_scale(self) -> float:
        return self.scale


def _net(gmv: float) -> float:
    return round(gmv, 2)


METRICS: dict[str, MetricDef] = {
    "gmv": MetricDef(
        "gmv", "销售额", "元",
        "已支付订单金额合计（orders.amount，status='paid'，按 order_date 过滤）",
        "SELECT COALESCE(SUM(amount),0) AS v FROM orders "
        "WHERE status='paid' AND order_date BETWEEN :s AND :e",
    ),
    "orders": MetricDef(
        "orders", "订单量", "单",
        "已支付订单数（orders 中 status='paid' 的记录数）",
        "SELECT COUNT(*) AS v FROM orders "
        "WHERE status='paid' AND order_date BETWEEN :s AND :e",
    ),
    "uv": MetricDef(
        "uv", "访客数", "人",
        "日流量表 visitors 求和（traffic_daily，按 stat_date 过滤）",
        "SELECT COALESCE(SUM(visitors),0) AS v FROM traffic_daily "
        "WHERE stat_date BETWEEN :s AND :e",
    ),
    "cvr": MetricDef(
        "cvr", "转化率", "%",
        "订单量 ÷ 访客数（分子见 orders，分母见 uv）",
        "SELECT CASE WHEN u.v>0 THEN CAST(o.v AS REAL)/u.v ELSE 0 END AS v FROM "
        "(SELECT COALESCE(SUM(visitors),0) v FROM traffic_daily WHERE stat_date BETWEEN :s AND :e) u, "
        "(SELECT COUNT(*) v FROM orders WHERE status='paid' AND order_date BETWEEN :s AND :e) o",
        scale=100.0,
    ),
    "aov": MetricDef(
        "aov", "客单价", "元",
        "销售额 ÷ 订单量（分子见 gmv，分母见 orders）",
        "SELECT CASE WHEN c.v>0 THEN g.v/c.v ELSE 0 END AS v FROM "
        "(SELECT COALESCE(SUM(amount),0) v FROM orders WHERE status='paid' AND order_date BETWEEN :s AND :e) g, "
        "(SELECT COUNT(*) v FROM orders WHERE status='paid' AND order_date BETWEEN :s AND :e) c",
    ),
    "stockout": MetricDef(
        "stockout", "缺货 SKU 数", "个",
        "inventory 中 stock_qty<=warning_threshold 的 SKU 数（snapshot_date 取区间末）",
        "SELECT COUNT(*) AS v FROM inventory WHERE stock_qty<=warning_threshold "
        "AND snapshot_date<=:e AND snapshot_date>=:s",
    ),
    "refund_rate": MetricDef(
        "refund_rate", "退款金额占比", "%",
        "退款金额 ÷ 销售额（refunds.amount ÷ orders 已支付金额）",
        "SELECT CASE WHEN g.v>0 THEN r.v/g.v ELSE 0 END AS v FROM "
        "(SELECT COALESCE(SUM(amount),0) v FROM refunds WHERE refund_date BETWEEN :s AND :e) r, "
        "(SELECT COALESCE(SUM(amount),0) v FROM orders WHERE status='paid' AND order_date BETWEEN :s AND :e) g",
        scale=100.0,
    ),
    "rating": MetricDef(
        "rating", "平均评分", "分",
        "reviews.rating 算术平均（按 review_date 过滤）",
        "SELECT COALESCE(AVG(rating),0) AS v FROM reviews WHERE review_date BETWEEN :s AND :e",
    ),
    "competitor_price": MetricDef(
        "competitor_price", "竞品均价", "元",
        "competitor_prices.price 算术平均（按 snapshot_date 过滤）",
        "SELECT COALESCE(AVG(price),0) AS v FROM competitor_prices "
        "WHERE snapshot_date BETWEEN :s AND :e",
    ),
    "refund_amount_avg": MetricDef(
        "refund_amount_avg", "单笔退款金额", "元",
        "退款总额 ÷ 退款笔数（refunds.amount 与 refunds 记录数）",
        "SELECT CASE WHEN c.v>0 THEN r.v/c.v ELSE 0 END AS v FROM "
        "(SELECT COALESCE(SUM(amount),0) v FROM refunds WHERE refund_date BETWEEN :s AND :e) r, "
        "(SELECT COUNT(*) v FROM refunds WHERE refund_date BETWEEN :s AND :e) c",
    ),
}


def data_coverage(engine: Engine) -> tuple[str | None, str | None]:
    """业务库实际数据覆盖区间。用于在用户问及无数据区间时给出可读提示。"""
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT MIN(order_date), MAX(order_date) FROM orders WHERE status='paid'"
            )).first()
    except Exception:  # noqa: BLE001
        return None, None
    if not row or not row[0]:
        return None, None
    return str(row[0]), str(row[1])


def metric_value(engine: Engine, key: str, period: Period) -> float | None:
    """按口径取值。

    关键区分：「区间内没有记录」返回 None，「读到了但合计为 0」返回 0.0。
    这两个不是一回事，混在一起会把"没数据"误判成"业务归零"。
    """
    mdef = METRICS.get(key)
    if mdef is None:
        return None
    # 先判断区间内是否存在记录：无记录 → None，而不是 COALESCE 出来的 0
    if metric_row_count(engine, key, period) == 0:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(mdef.sql),
                {"s": period.start.isoformat(), "e": period.end.isoformat()},
            ).first()
    except Exception:  # noqa: BLE001
        return None
    if row is None or row[0] is None:
        return None
    return float(row[0]) * mdef.scale


def metric_row_count(engine: Engine, key: str, period: Period) -> int:
    """样本量，用于置信度判定（FR-6.5）。"""
    sql = {
        "gmv": "SELECT COUNT(*) FROM orders WHERE status='paid' AND order_date BETWEEN :s AND :e",
        "orders": "SELECT COUNT(*) FROM orders WHERE status='paid' AND order_date BETWEEN :s AND :e",
        "uv": "SELECT COUNT(*) FROM traffic_daily WHERE stat_date BETWEEN :s AND :e",
        "cvr": "SELECT COUNT(*) FROM orders WHERE status='paid' AND order_date BETWEEN :s AND :e",
        "aov": "SELECT COUNT(*) FROM orders WHERE status='paid' AND order_date BETWEEN :s AND :e",
        "stockout": "SELECT COUNT(*) FROM inventory WHERE snapshot_date BETWEEN :s AND :e",
        "refund_rate": "SELECT COUNT(*) FROM refunds WHERE refund_date BETWEEN :s AND :e",
        "rating": "SELECT COUNT(*) FROM reviews WHERE review_date BETWEEN :s AND :e",
        "competitor_price": "SELECT COUNT(*) FROM competitor_prices WHERE snapshot_date BETWEEN :s AND :e",
        "refund_amount_avg": "SELECT COUNT(*) FROM refunds WHERE refund_date BETWEEN :s AND :e",
    }.get(key)
    if not sql:
        return 0
    try:
        with engine.connect() as conn:
            row = conn.execute(text(sql), {"s": period.start.isoformat(),
                                           "e": period.end.isoformat()}).first()
    except Exception:  # noqa: BLE001
        return 0
    return int(row[0]) if row else 0


def refund_concentration(engine: Engine, p1: Period) -> list[dict[str, Any]]:
    """按类目拆分退款金额，用于判断退款是否集中在单一类目。"""
    sql = (
        "SELECT c.category_name AS cat, COALESCE(SUM(r.amount),0) AS amt, COUNT(*) AS cnt "
        "FROM refunds r JOIN orders o ON o.order_id=r.order_id "
        "JOIN products p ON p.product_id=o.product_id "
        "JOIN categories c ON c.category_id=p.category_id "
        "WHERE r.refund_date BETWEEN :s AND :e "
        "GROUP BY c.category_name ORDER BY amt DESC"
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(sql), {"s": p1.start.isoformat(),
                                            "e": p1.end.isoformat()}).all()
    except Exception:  # noqa: BLE001
        return []
    return [{"category": r[0], "amount": round(float(r[1]), 2), "count": int(r[2])}
            for r in rows]


def category_contributions(engine: Engine, p1: Period, p0: Period) -> list[dict[str, Any]]:
    """按类目做精确加性分解：Σ(本期-基期) 恰好等于总差额。"""
    sql = (
        "SELECT c.category_name AS cat, "
        "  COALESCE(SUM(CASE WHEN o.order_date BETWEEN :s1 AND :e1 THEN o.amount END),0) AS cur, "
        "  COALESCE(SUM(CASE WHEN o.order_date BETWEEN :s0 AND :e0 THEN o.amount END),0) AS base "
        "FROM orders o JOIN products p ON p.product_id=o.product_id "
        "JOIN categories c ON c.category_id=p.category_id "
        "WHERE o.status='paid' AND (o.order_date BETWEEN :s1 AND :e1 OR o.order_date BETWEEN :s0 AND :e0) "
        "GROUP BY c.category_name ORDER BY (cur-base) ASC"
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(sql), {
                "s1": p1.start.isoformat(), "e1": p1.end.isoformat(),
                "s0": p0.start.isoformat(), "e0": p0.end.isoformat(),
            }).all()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        cur, base = float(r[1]), float(r[2])
        out.append({"category": r[0], "current": _net(cur), "baseline": _net(base),
                    "delta": _net(cur - base)})
    return out
