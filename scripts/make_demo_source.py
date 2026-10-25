"""在项目 data/ 下造一个小型演示库，供"页面登记数据源"截图使用。

结构只覆盖必需表 + 一张可选表，故意缺 3 张可选表，
以便探查结果能展示"缺可选表不影响主流程"的说明。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "demo_source.db"


def main() -> int:
    if OUT.exists():
        OUT.unlink()
    con = sqlite3.connect(str(OUT))
    try:
        con.executescript("""
        CREATE TABLE categories (category_id INTEGER PRIMARY KEY, category_name TEXT);
        CREATE TABLE products (product_id INTEGER PRIMARY KEY, product_name TEXT,
                               category_id INTEGER, price REAL, cost REAL);
        CREATE TABLE orders (order_id INTEGER PRIMARY KEY, product_id INTEGER,
                             order_date TEXT, amount REAL, status TEXT);
        CREATE TABLE traffic_daily (stat_date TEXT PRIMARY KEY, visitors INTEGER);
        CREATE TABLE refunds (refund_id INTEGER PRIMARY KEY, order_id INTEGER,
                              refund_date TEXT, amount REAL, reason_id INTEGER);
        CREATE TABLE reviews (review_id INTEGER PRIMARY KEY, product_id INTEGER,
                              review_date TEXT, rating INTEGER);
        CREATE TABLE inventory (snapshot_date TEXT, sku_id INTEGER, product_id INTEGER,
                                stock_qty INTEGER, warning_threshold INTEGER);
        CREATE TABLE competitor_prices (snapshot_date TEXT, competitor_name TEXT,
                                        category_id INTEGER, price REAL);
        INSERT INTO categories VALUES (1, '演示类目');
        INSERT INTO products VALUES (1, '演示商品A', 1, 199, 80);
        INSERT INTO products VALUES (2, '演示商品B', 1, 299, 130);
        INSERT INTO traffic_daily VALUES ('2024-05-01', 500);
        INSERT INTO traffic_daily VALUES ('2024-06-01', 520);
        INSERT INTO orders VALUES (1, 1, '2024-05-10', 199, 'paid');
        INSERT INTO orders VALUES (2, 2, '2024-06-10', 299, 'paid');
        INSERT INTO competitor_prices VALUES ('2024-05-06', '竞品A', 1, 210);
        INSERT INTO competitor_prices VALUES ('2024-06-03', '竞品A', 1, 215);
        INSERT INTO inventory VALUES ('2024-06-03', 1, 1, 100, 30);
        INSERT INTO reviews VALUES (1, 1, '2024-05-11', 5);
        INSERT INTO reviews VALUES (2, 1, '2024-06-11', 4);
        INSERT INTO refunds VALUES (1, 1, '2024-05-20', 199, 1);
        """)
        con.commit()

        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        print(f"已生成 {OUT}")
        print(f"表（{len(tables)}）：{', '.join(tables)}")
        print("说明：缺 traffic_channel / refund_reasons / search_exposure "
              "三张可选表，探查页会展示对应影响说明。")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
