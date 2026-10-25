"""核对示例数据是否真的呈现预期形态（不预埋结论，但要有可发现的变化）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from app import config  # noqa: E402

eng = create_engine(f"sqlite+pysqlite:///{config.BIZ_DB.as_posix()}", future=True)


def q(sql, **kw):
    with eng.connect() as c:
        return c.execute(text(sql), kw).all()


print("=== 月度 GMV / 订单 / 访客 / 转化率 ===")
for m in ("2024-04", "2024-05", "2024-06"):
    s, e = f"{m}-01", f"{m}-30"
    gmv = q("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' "
            "AND order_date BETWEEN :s AND :e", s=s, e=e)[0][0]
    orders = q("SELECT COUNT(*) FROM orders WHERE status='paid' "
               "AND order_date BETWEEN :s AND :e", s=s, e=e)[0][0]
    uv = q("SELECT COALESCE(SUM(visitors),0) FROM traffic_daily "
           "WHERE stat_date BETWEEN :s AND :e", s=s, e=e)[0][0]
    cvr = orders / uv * 100 if uv else 0
    aov = gmv / orders if orders else 0
    print(f"{m}  GMV={gmv:>10.2f}  订单={orders:>6d}  UV={uv:>7d}  "
          f"CVR={cvr:5.2f}%  AOV={aov:7.2f}")

print("\n=== 5月 vs 6月 类目分解（GMV 差额） ===")
rows = q("""
SELECT c.category_name,
  COALESCE(SUM(CASE WHEN o.order_date BETWEEN '2024-06-01' AND '2024-06-30'
                    THEN o.amount END),0) AS cur,
  COALESCE(SUM(CASE WHEN o.order_date BETWEEN '2024-05-01' AND '2024-05-30'
                    THEN o.amount END),0) AS base
FROM orders o JOIN products p ON p.product_id=o.product_id
JOIN categories c ON c.category_id=p.category_id
WHERE o.status='paid'
GROUP BY c.category_name ORDER BY (cur-base) ASC
""")
tot = 0.0
for name, cur, base in rows:
    print(f"{name:10s} 6月={cur:>10.2f}  5月={base:>10.2f}  差={cur-base:>+10.2f}")
    tot += cur - base
print(f"{'合计差额':10s} {tot:+.2f}")

print("\n=== 竞品均价 5月 vs 6月（期望：不下探） ===")
for m in ("2024-05", "2024-06"):
    v = q("SELECT COALESCE(AVG(price),0) FROM competitor_prices "
          "WHERE snapshot_date BETWEEN :s AND :e",
          s=f"{m}-01", e=f"{m}-30")[0][0]
    print(f"{m} 竞品均价 = {v:.2f}")

print("\n=== 退款占比 5月 vs 6月 ===")
for m in ("2024-05", "2024-06"):
    s, e = f"{m}-01", f"{m}-30"
    r = q("SELECT COALESCE(SUM(amount),0) FROM refunds "
          "WHERE refund_date BETWEEN :s AND :e", s=s, e=e)[0][0]
    g = q("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' "
          "AND order_date BETWEEN :s AND :e", s=s, e=e)[0][0]
    print(f"{m} 退款金额={r:>9.2f}  占比={r/g*100:5.2f}%")

print("\n=== 评分 5月 vs 6月 ===")
for m in ("2024-05", "2024-06"):
    v = q("SELECT COALESCE(AVG(rating),0) FROM reviews "
          "WHERE review_date BETWEEN :s AND :e",
          s=f"{m}-01", e=f"{m}-30")[0][0]
    print(f"{m} 平均评分 = {v:.3f}")

print("\n=== 缺货SKU 5月 vs 6月 ===")
for m in ("2024-05", "2024-06"):
    v = q("SELECT COUNT(*) FROM inventory WHERE stock_qty<=warning_threshold "
          "AND snapshot_date BETWEEN :s AND :e",
          s=f"{m}-01", e=f"{m}-30")[0][0]
    print(f"{m} 缺货SKU数 = {v}")

print("\n=== 类目3（服饰鞋包）转化率 6月上/下半月 ===")
for label, s, e in (("6月1-9日", "2024-06-01", "2024-06-09"),
                    ("6月10-30日", "2024-06-10", "2024-06-30")):
    o = q("""SELECT COUNT(*) FROM orders o JOIN products p ON p.product_id=o.product_id
             WHERE p.category_id=3 AND o.status='paid' AND o.order_date BETWEEN :s AND :e""",
          s=s, e=e)[0][0]
    c_ = q("""SELECT COALESCE(SUM(clicks),0) FROM search_exposure se
              JOIN products p ON p.product_id=se.product_id
              WHERE p.category_id=3 AND se.stat_date BETWEEN :s AND :e""", s=s, e=e)[0][0]
    print(f"{label:14s} 订单={o:>5d}  点击={c_:>7d}  转化={o/c_*100 if c_ else 0:5.2f}%")

eng.dispose()
