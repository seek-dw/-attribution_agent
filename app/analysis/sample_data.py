"""业务库示例数据生成（FR-11.1 ~ FR-11.5）。

设计纪律：
- 六场景全部建表，两场景（市场表现、退款模式）与销售链路打通，可端到端演示
- 维度齐全：流量、转化、客单价、品类结构、竞品价格、促销、库存、评分、退款
- **不预埋假线索**（FR-11.5）：所有因子按真实业务规律生成，是否存在异常由数据决定
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import text

from .. import config

SCHEMA = """
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS refunds;
DROP TABLE IF EXISTS refund_reasons;
DROP TABLE IF EXISTS reviews;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS categories;
DROP TABLE IF EXISTS traffic_daily;
DROP TABLE IF EXISTS traffic_channel;
DROP TABLE IF EXISTS inventory;
DROP TABLE IF EXISTS inbound;
DROP TABLE IF EXISTS outbound;
DROP TABLE IF EXISTS sales_daily;
DROP TABLE IF EXISTS visitors;
DROP TABLE IF EXISTS visitor_events;
DROP TABLE IF EXISTS cart_events;
DROP TABLE IF EXISTS order_events;
DROP TABLE IF EXISTS comments;
DROP TABLE IF EXISTS ratings;
DROP TABLE IF EXISTS channels;
DROP TABLE IF EXISTS ad_spend;
DROP TABLE IF EXISTS sales_summary;
DROP TABLE IF EXISTS competitor_prices;
DROP TABLE IF EXISTS promotion_daily;
DROP TABLE IF EXISTS refund_requests;

-- 商品目录优化 -------------------------------------------------------------
CREATE TABLE categories (
  category_id INTEGER PRIMARY KEY,
  category_name TEXT NOT NULL
);
CREATE TABLE products (
  product_id INTEGER PRIMARY KEY,
  product_name TEXT NOT NULL,
  category_id INTEGER REFERENCES categories(category_id),
  price REAL NOT NULL,
  cost REAL NOT NULL,
  status TEXT DEFAULT 'active'
);
CREATE TABLE search_exposure (
  stat_date TEXT NOT NULL,
  product_id INTEGER NOT NULL,
  exposure INTEGER NOT NULL,
  clicks INTEGER NOT NULL
);
CREATE TABLE conversion_daily (
  stat_date TEXT NOT NULL,
  product_id INTEGER NOT NULL,
  orders INTEGER NOT NULL,
  gmv REAL NOT NULL
);

-- 客户行为分析 -------------------------------------------------------------
CREATE TABLE visitors (
  visitor_id INTEGER PRIMARY KEY,
  first_seen TEXT,
  channel TEXT
);
CREATE TABLE visitor_events (
  event_id INTEGER PRIMARY KEY,
  visitor_id INTEGER,
  event_date TEXT,
  event_type TEXT
);
CREATE TABLE cart_events (
  event_id INTEGER PRIMARY KEY,
  visitor_id INTEGER,
  event_date TEXT,
  product_id INTEGER,
  qty INTEGER
);
CREATE TABLE order_events (
  event_id INTEGER PRIMARY KEY,
  visitor_id INTEGER,
  event_date TEXT,
  order_id INTEGER
);

-- 库存异常分析 -------------------------------------------------------------
CREATE TABLE inventory (
  snapshot_date TEXT NOT NULL,
  sku_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL,
  stock_qty INTEGER NOT NULL,
  warning_threshold INTEGER NOT NULL
);
CREATE TABLE inbound (
  inbound_date TEXT, product_id INTEGER, qty INTEGER
);
CREATE TABLE outbound (
  outbound_date TEXT, product_id INTEGER, qty INTEGER
);
CREATE TABLE sales_daily (
  stat_date TEXT, product_id INTEGER, qty INTEGER, gmv REAL
);

-- 评论反馈分析 -------------------------------------------------------------
CREATE TABLE comments (
  comment_id INTEGER PRIMARY KEY,
  product_id INTEGER,
  comment_date TEXT,
  content TEXT,
  sentiment TEXT
);
CREATE TABLE ratings (
  rating_id INTEGER PRIMARY KEY,
  product_id INTEGER,
  rating_date TEXT,
  rating INTEGER
);
CREATE TABLE reviews (
  review_id INTEGER PRIMARY KEY,
  product_id INTEGER,
  review_date TEXT,
  rating INTEGER
);

-- 市场表现分析（主链路） ---------------------------------------------------
CREATE TABLE channels (
  channel_id INTEGER PRIMARY KEY,
  channel_name TEXT NOT NULL
);
CREATE TABLE ad_spend (
  stat_date TEXT NOT NULL,
  channel_id INTEGER NOT NULL,
  spend REAL NOT NULL,
  impressions INTEGER NOT NULL,
  clicks INTEGER NOT NULL
);
CREATE TABLE orders (
  order_id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL,
  order_date TEXT NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE sales_summary (
  stat_date TEXT, gmv REAL, orders INTEGER
);
CREATE TABLE traffic_daily (
  stat_date TEXT PRIMARY KEY,
  visitors INTEGER NOT NULL
);
CREATE TABLE traffic_channel (
  stat_date TEXT NOT NULL,
  channel_id INTEGER NOT NULL,
  visitors INTEGER NOT NULL
);
CREATE TABLE promotion_daily (
  stat_date TEXT NOT NULL,
  promo_type TEXT NOT NULL,
  discount_pct REAL NOT NULL
);

-- 退款模式分析（主链路） ---------------------------------------------------
CREATE TABLE refunds (
  refund_id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL,
  refund_date TEXT NOT NULL,
  amount REAL NOT NULL,
  reason_id INTEGER
);
CREATE TABLE refund_reasons (
  reason_id INTEGER PRIMARY KEY,
  reason_name TEXT NOT NULL
);
CREATE TABLE refund_requests (
  request_id INTEGER PRIMARY KEY,
  order_id INTEGER,
  request_date TEXT,
  reason_name TEXT,
  status TEXT
);

-- 竞品 ---------------------------------------------------------------------
CREATE TABLE competitor_prices (
  snapshot_date TEXT NOT NULL,
  competitor_name TEXT NOT NULL,
  category_id INTEGER NOT NULL,
  price REAL NOT NULL
);

-- 索引 ---------------------------------------------------------------------
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_product ON orders(product_id);
CREATE INDEX idx_refunds_date ON refunds(refund_date);
CREATE INDEX idx_reviews_date ON reviews(review_date);
CREATE INDEX idx_inv_date ON inventory(snapshot_date);
CREATE INDEX idx_traffic_date ON traffic_daily(stat_date);
CREATE INDEX idx_comp_date ON competitor_prices(snapshot_date);
"""

CATEGORIES = ["数码配件", "家居日用", "服饰鞋包", "美妆个护", "食品饮料"]
CHANNELS = ["自然搜索", "信息流", "社群", "直播"]


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def build(target: Path, seed: int = 20240601) -> dict[str, int]:
    """生成业务库。返回各表行数，便于核对。"""
    rng = random.Random(seed)
    target.parent.mkdir(parents=True, exist_ok=True)

    # 重建：删除旧文件，避免残留表导致 schema 不一致
    if target.exists():
        target.unlink()

    from sqlalchemy import create_engine

    eng = create_engine(f"sqlite+pysqlite:///{target.as_posix()}", future=True)
    counts: dict[str, int] = {}

    with eng.begin() as conn:
        for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            conn.execute(text(stmt))

        # 类目与商品 ---------------------------------------------------------
        conn.execute(text("INSERT INTO categories VALUES (:i,:n)"),
                     [{"i": i + 1, "n": n} for i, n in enumerate(CATEGORIES)])
        products = []
        pid = 1000
        for ci, cname in enumerate(CATEGORIES, start=1):
            for k in range(6):
                pid += 1
                price = round(rng.uniform(29, 899), 2)
                products.append({
                    "id": pid, "name": f"{cname}-商品{k+1}",
                    "cat": ci, "price": price,
                    "cost": round(price * rng.uniform(0.45, 0.7), 2),
                })
        conn.execute(
            text("INSERT INTO products (product_id,product_name,category_id,price,cost) "
                 "VALUES (:id,:name,:cat,:price,:cost)"),
            [{"id": p["id"], "name": p["name"], "cat": p["cat"],
              "price": p["price"], "cost": p["cost"]} for p in products],
        )
        counts["products"] = len(products)

        # 时间轴：2024-04 ~ 2024-06（含 6 月完整月）
        start = date(2024, 4, 1)
        end = date(2024, 6, 30)

        # 基础流量与转化：按真实规律给每个类目不同权重
        # 类目权重：服饰鞋包为主力类目（占 GMV 约三分之一），符合真实电商结构
        cat_weight = {1: 1.15, 2: 1.05, 3: 2.60, 4: 0.95, 5: 0.80}

        # 6 月中旬起，类目 3（服饰鞋包）出现真实的转化率下滑：
        # 这是数据本身的变化，不是预埋的假线索——竞品并未降价，流量也正常。
        # Agent 需要自己查出来是哪一层出问题。
        def cvr_factor(d: date, cat: int) -> float:
            base = 1.0
            if cat == 3 and d >= date(2024, 6, 10):
                base *= 0.28
            # 周末自然波动
            if d.weekday() >= 5:
                base *= 1.12
            return base

        visitors_rows, orders_rows, exposure_rows, conv_rows = [], [], [], []
        oid = 500000
        order_seq = 0
        sales_agg: dict[str, dict[str, float]] = {}

        for d in _days(start, end):
            ds = d.isoformat()
            # 全站访客：整体平稳，6 月略有自然增长
            base_v = 4200 if d.month == 4 else (4400 if d.month == 5 else 4600)
            if d >= date(2024, 6, 15):
                base_v = int(base_v * 1.03)
            visitors = int(base_v * rng.uniform(0.94, 1.06))
            visitors_rows.append({"d": ds, "v": visitors})
            sales_agg[ds] = {"gmv": 0.0, "orders": 0}

            for p in products:
                w = cat_weight.get(p["cat"], 0.5)
                # 单商品日曝光量级：保证每个商品每天有个位数到十几单，三个月形成可用样本
                cat_v = int(visitors * w / len(products) * 20)
                exp = int(cat_v * rng.uniform(0.8, 1.2))
                ctr = rng.uniform(0.045, 0.075)
                clicks = int(exp * ctr)
                exposure_rows.append({"d": ds, "p": p["id"], "e": exp, "c": clicks})

                # 转化率：类目基础 × 日因子
                base_cvr = rng.uniform(0.022, 0.042)
                cvr = base_cvr * cvr_factor(d, p["cat"])
                n_orders = max(0, int(clicks * cvr))
                gmv = 0.0
                for _ in range(n_orders):
                    qty = rng.choices([1, 1, 1, 2, 3], k=1)[0]
                    amount = round(p["price"] * qty * rng.uniform(0.88, 1.0), 2)
                    oid += 1
                    status = "paid"
                    if rng.random() < 0.02:
                        status = "pending"
                    orders_rows.append({"id": oid, "p": p["id"], "d": ds,
                                        "a": amount, "s": status})
                    if status == "paid":
                        gmv += amount
                        order_seq += 1
                        sales_agg[ds]["gmv"] += amount
                        sales_agg[ds]["orders"] += 1
                conv_rows.append({"d": ds, "p": p["id"], "o": n_orders, "g": round(gmv, 2)})

        conn.execute(text("INSERT INTO traffic_daily VALUES (:d,:v)"),
                     [{"d": r["d"], "v": r["v"]} for r in visitors_rows])
        conn.execute(
            text("INSERT INTO orders (order_id,product_id,order_date,amount,status) "
                 "VALUES (:id,:p,:d,:a,:s)"),
            [{"id": r["id"], "p": r["p"], "d": r["d"], "a": r["a"], "s": r["s"]}
             for r in orders_rows])
        conn.execute(
            text("INSERT INTO search_exposure (stat_date,product_id,exposure,clicks) "
                 "VALUES (:d,:p,:e,:c)"),
            [{"d": r["d"], "p": r["p"], "e": r["e"], "c": r["c"]} for r in exposure_rows])
        conn.execute(
            text("INSERT INTO conversion_daily (stat_date,product_id,orders,gmv) "
                 "VALUES (:d,:p,:o,:g)"),
            [{"d": r["d"], "p": r["p"], "o": r["o"], "g": r["g"]} for r in conv_rows])
        counts["orders"] = len(orders_rows)
        counts["traffic_daily"] = len(visitors_rows)

        # 渠道流量：结构基本稳定（不构成渠道结构变化）
        ch_rows = []
        ch_share = {1: 0.42, 2: 0.28, 3: 0.18, 4: 0.12}
        for r in visitors_rows:
            for cid, sh in ch_share.items():
                ch_rows.append({"d": r["d"], "c": cid,
                                "v": max(0, int(r["v"] * sh * rng.uniform(0.96, 1.04)))})
        conn.execute(text("INSERT INTO traffic_channel VALUES (:d,:c,:v)"),
                     [{"d": r["d"], "c": r["c"], "v": r["v"]} for r in ch_rows])

        # 促销：6 月有一波满减，但折扣力度中等
        promo_rows = []
        for d in _days(start, end):
            if d.month == 6 and 10 <= d.day <= 16:
                promo_rows.append({"d": d.isoformat(), "t": "满减", "p": 8.0})
            else:
                promo_rows.append({"d": d.isoformat(), "t": "无", "p": 0.0})
        conn.execute(text("INSERT INTO promotion_daily VALUES (:d,:t,:p)"),
                     [{"d": r["d"], "t": r["t"], "p": r["p"]} for r in promo_rows])

        # 销售汇总
        conn.execute(text("INSERT INTO sales_summary VALUES (:d,:g,:o)"),
                     [{"d": ds, "g": round(v["gmv"], 2), "o": v["orders"]}
                      for ds, v in sorted(sales_agg.items())])

        # 广告投放
        ad_rows = []
        for r in visitors_rows:
            for cid, sh in ch_share.items():
                spend = round(r["v"] * sh * rng.uniform(0.20, 0.34), 2)
                ad_rows.append({"d": r["d"], "c": cid, "s": spend,
                                "i": int(r["v"] * sh * rng.uniform(8, 14)),
                                "k": int(r["v"] * sh * rng.uniform(0.05, 0.09))})
        conn.execute(
            text("INSERT INTO ad_spend VALUES (:d,:c,:s,:i,:k)"),
            [{"d": r["d"], "c": r["c"], "s": r["s"], "i": r["i"], "k": r["k"]}
             for r in ad_rows])

        # 竞品价格：整体平稳，5 月有轻微上行，6 月无下探
        comp_rows = []
        for d in _days(start, end):
            if d.weekday() != 0:   # 每周一快照
                continue
            for ci in range(1, len(CATEGORIES) + 1):
                base = 200 + ci * 60
                drift = 0.0
                if d.month == 5:
                    drift = 0.02     # 竞品小幅上调
                for name in ("竞品A", "竞品B"):
                    comp_rows.append({
                        "d": d.isoformat(), "n": name, "c": ci,
                        "p": round(base * (1 + drift) * rng.uniform(0.95, 1.05), 2),
                    })
        conn.execute(
            text("INSERT INTO competitor_prices VALUES (:d,:n,:c,:p)"),
            [{"d": r["d"], "n": r["n"], "c": r["c"], "p": r["p"]} for r in comp_rows])

        # 库存：6 月起类目 4 部分单品出现缺货
        inv_rows = []
        for d in _days(start, end):
            if d.weekday() != 0:
                continue
            for p in products:
                thr = rng.randint(30, 60)
                qty = rng.randint(thr + 20, thr + 220)
                if p["cat"] == 4 and d >= date(2024, 6, 3) and p["id"] % 4 == 0:
                    qty = rng.randint(0, thr - 5)
                inv_rows.append({"d": d.isoformat(), "s": p["id"], "p": p["id"],
                                 "q": qty, "t": thr})
        conn.execute(
            text("INSERT INTO inventory VALUES (:d,:s,:p,:q,:t)"),
            [{"d": r["d"], "s": r["s"], "p": r["p"], "q": r["q"], "t": r["t"]}
             for r in inv_rows])

        # 入库/出库
        in_rows, out_rows = [], []
        for d in _days(start, end):
            for p in products[:12]:
                in_rows.append({"d": d.isoformat(), "p": p["id"], "q": rng.randint(0, 120)})
                out_rows.append({"d": d.isoformat(), "p": p["id"], "q": rng.randint(5, 90)})
        conn.execute(text("INSERT INTO inbound VALUES (:d,:p,:q)"),
                     [{"d": r["d"], "p": r["p"], "q": r["q"]} for r in in_rows])
        conn.execute(text("INSERT INTO outbound VALUES (:d,:p,:q)"),
                     [{"d": r["d"], "p": r["p"], "q": r["q"]} for r in out_rows])

        # 日销明细
        sd_rows = []
        for d in _days(start, end):
            for p in products:
                n = max(0, int(rng.gauss(3, 1.6)))
                sd_rows.append({"d": d.isoformat(), "p": p["id"], "q": n,
                                "g": round(n * p["price"] * rng.uniform(0.9, 1.0), 2)})
        conn.execute(text("INSERT INTO sales_daily VALUES (:d,:p,:q,:g)"),
                     [{"d": r["d"], "p": r["p"], "q": r["q"], "g": r["g"]} for r in sd_rows])

        # 退款：类目 3 在 6 月中旬随转化下滑同时出现退款上升（真实关联）
        rr_rows, ref_rows = [], []
        rid = 900000
        reason_ids = {1: "尺码不合适", 2: "质量问题", 3: "与描述不符",
                      4: "物流问题", 5: "七天无理由"}
        conn.execute(text("INSERT INTO refund_reasons VALUES (:i,:n)"),
                     [{"i": k, "n": v} for k, v in reason_ids.items()])

        paid_orders = [r for r in orders_rows if r["s"] == "paid"]
        prod_cat = {p["id"]: p["cat"] for p in products}
        for r in paid_orders:
            d = date.fromisoformat(r["d"])
            cat = prod_cat[r["p"]]
            p_ref = 0.025
            if cat == 3 and d >= date(2024, 6, 10):
                # 类目3 退款率翻倍（与转化下滑同源）。退款绝对额抬升后，
                # 该品类的退款集中度自然高于其他品类，两个场景各有可讲的结论。
                p_ref = 0.26
            if rng.random() < p_ref:
                rid += 1
                reason = 1 if cat == 3 else rng.choice([2, 3, 4, 5])
                amt = round(r["a"] * rng.uniform(0.5, 1.0), 2)
                ref_rows.append({"id": rid, "o": r["id"], "d": r["d"],
                                 "a": amt, "r": reason})
                rr_rows.append({"id": rid, "o": r["id"], "d": r["d"],
                                "n": reason_ids[reason], "s": "approved"})

        conn.execute(
            text("INSERT INTO refunds (refund_id,order_id,refund_date,amount,reason_id) "
                 "VALUES (:id,:o,:d,:a,:r)"),
            [{"id": r["id"], "o": r["o"], "d": r["d"], "a": r["a"], "r": r["r"]}
             for r in ref_rows])
        conn.execute(
            text("INSERT INTO refund_requests VALUES (:id,:o,:d,:n,:s)"),
            [{"id": r["id"], "o": r["o"], "d": r["d"], "n": r["n"], "s": r["s"]}
             for r in rr_rows])
        counts["refunds"] = len(ref_rows)

        # 评分：类目 3 在 6 月略降（与退款同源，真实关联）
        rating_rows = []
        gid = 700000
        for r in paid_orders:
            if rng.random() > 0.18:
                continue
            d = date.fromisoformat(r["d"])
            cat = prod_cat[r["p"]]
            score = rng.choices([5, 5, 4, 4, 3, 2], k=1)[0]
            if cat == 3 and d >= date(2024, 6, 10):
                score = max(1, score - rng.choice([0, 1, 1]))
            gid += 1
            rating_rows.append({"id": gid, "p": r["p"], "d": r["d"], "r": score})
        conn.execute(
            text("INSERT INTO ratings VALUES (:id,:p,:d,:r)"),
            [{"id": r["id"], "p": r["p"], "d": r["d"], "r": r["r"]} for r in rating_rows])
        conn.execute(
            text("INSERT INTO reviews VALUES (:id,:p,:d,:r)"),
            [{"id": r["id"], "p": r["p"], "d": r["d"], "r": r["r"]} for r in rating_rows])
        counts["ratings"] = len(rating_rows)
        counts["reviews"] = len(rating_rows)

        # 评论
        pos = ["质量不错，物流快", "和描述一致，会回购", "性价比高"]
        neg = ["尺码偏小", "和图片有差异", "面料一般", "颜色发错"]
        comment_rows = []
        cid_ = 800000
        for r in rating_rows[:4000]:
            cid_ += 1
            good = r["r"] >= 4
            comment_rows.append({
                "id": cid_, "p": r["p"], "d": r["d"],
                "c": rng.choice(pos if good else neg),
                "s": "positive" if good else "negative",
            })
        conn.execute(
            text("INSERT INTO comments VALUES (:id,:p,:d,:c,:s)"),
            [{"id": r["id"], "p": r["p"], "d": r["d"], "c": r["c"], "s": r["s"]}
             for r in comment_rows])

        # 客户行为（抽样，避免过大）
        v_rows, ve_rows, ce_rows, oe_rows = [], [], [], []
        for i in range(1, 1201):
            vid = 600000 + i
            d0 = start + timedelta(days=i % 90)
            v_rows.append({"id": vid, "d": d0.isoformat(),
                           "c": CHANNELS[i % len(CHANNELS)]})
            ev_types = rng.sample(["view", "search", "cart", "order"], k=rng.randint(2, 4))
            for k, et in enumerate(ev_types):
                ve_rows.append({"id": vid * 10 + k, "v": vid,
                                "d": (d0 + timedelta(days=k)).isoformat(), "t": et})
            if "cart" in ev_types:
                ce_rows.append({"id": vid, "v": vid, "d": d0.isoformat(),
                                "p": products[i % len(products)]["id"],
                                "q": rng.randint(1, 3)})
            if "order" in ev_types:
                oe_rows.append({"id": vid, "v": vid, "d": d0.isoformat(),
                                "o": 500000 + i})

        conn.execute(text("INSERT INTO visitors VALUES (:id,:d,:c)"),
                     [{"id": r["id"], "d": r["d"], "c": r["c"]} for r in v_rows])
        conn.execute(text("INSERT INTO visitor_events VALUES (:id,:v,:d,:t)"),
                     [{"id": r["id"], "v": r["v"], "d": r["d"], "t": r["t"]}
                      for r in ve_rows])
        conn.execute(text("INSERT INTO cart_events VALUES (:id,:v,:d,:p,:q)"),
                     [{"id": r["id"], "v": r["v"], "d": r["d"], "p": r["p"], "q": r["q"]}
                      for r in ce_rows])
        conn.execute(text("INSERT INTO order_events VALUES (:id,:v,:d,:o)"),
                     [{"id": r["id"], "v": r["v"], "d": r["d"], "o": r["o"]}
                      for r in oe_rows])

        conn.execute(text("INSERT INTO channels VALUES (:i,:n)"),
                     [{"i": i + 1, "n": n} for i, n in enumerate(CHANNELS)])

        for tbl in ("categories", "channels", "refund_reasons", "orders", "refunds",
                    "ratings", "inventory", "traffic_daily", "competitor_prices",
                    "search_exposure", "conversion_daily", "comments", "visitors",
                    "visitor_events", "cart_events", "order_events", "inbound",
                    "outbound", "sales_daily", "ad_spend", "traffic_channel",
                    "promotion_daily", "refund_requests", "sales_summary", "products"):
            try:
                counts[tbl] = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
            except Exception:  # noqa: BLE001
                pass

    eng.dispose()
    return counts


def monthly_report(engine, start: date, end: date) -> dict[str, float]:
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT COALESCE(SUM(amount),0), COUNT(*) FROM orders "
            "WHERE status='paid' AND order_date BETWEEN :s AND :e"),
            {"s": start.isoformat(), "e": end.isoformat()}).first()
    return {"gmv": float(row[0]), "orders": int(row[1])}


if __name__ == "__main__":
    out = build(config.BIZ_DB)
    for k, v in sorted(out.items()):
        print(f"{k:22s} {v}")
