"""数据源接入实测：造一个自己的 SQLite 库，接进来，验证分析真的读它。

用法: python scripts/demo_datasource.py [base_url]
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "my_business.db"


class C:
    def __init__(self):
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _do(self, method, path, data=None, form=None):
        headers, body = {}, None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(BASE + path, data=body, headers=headers,
                                     method=method)
        try:
            with self.op.open(req, timeout=180) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw)
                except json.JSONDecodeError:
                    return r.status, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw.decode("utf-8", "replace")

    def get(self, p):
        return self._do("GET", p)

    def post(self, p, **kw):
        return self._do("POST", p, **kw)

    def delete(self, p):
        return self._do("DELETE", p)


def build_my_db(path: Path) -> None:
    """造一个结构和示例库一致、但数字完全不同的库，便于区分读的是哪一个。"""
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
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
        CREATE TABLE promotion_daily (stat_date TEXT, promo_type TEXT,
                                      discount_pct REAL);
        """)
        con.execute("INSERT INTO categories VALUES (1, '女装')")
        con.execute("INSERT INTO categories VALUES (2, '男装')")
        con.execute("INSERT INTO products VALUES (1, '连衣裙', 1, 299, 120)")
        con.execute("INSERT INTO products VALUES (2, '衬衫', 2, 199, 80)")

        # 5 月：女装卖得好；6 月：女装断崖式下滑（转化崩塌），男装持平
        rows = []
        oid = 0
        for d in range(1, 31):
            ds5 = f"2024-05-{d:02d}"
            ds6 = f"2024-06-{d:02d}"
            for _ in range(12):          # 5 月女装每天 12 单
                oid += 1
                rows.append((oid, 1, ds5, 299.0, "paid"))
            for _ in range(3):           # 6 月女装每天 3 单
                oid += 1
                rows.append((oid, 1, ds6, 299.0, "paid"))
            for _ in range(8):           # 男装两月持平
                oid += 1
                rows.append((oid, 2, ds5, 199.0, "paid"))
                oid += 1
                rows.append((oid, 2, ds6, 199.0, "paid"))
            con.execute("INSERT INTO traffic_daily VALUES (?, ?)", (ds5, 1200))
            con.execute("INSERT INTO traffic_daily VALUES (?, ?)", (ds6, 1250))
            # 竞品价格：6 月略涨，不构成"竞品挤压"
            con.execute("INSERT INTO competitor_prices VALUES (?,?,?,?)",
                        (ds5, "竞品A", 1, 310.0))
            con.execute("INSERT INTO competitor_prices VALUES (?,?,?,?)",
                        (ds6, "竞品A", 1, 320.0))
            con.execute("INSERT INTO inventory VALUES (?,?,?,?,?)",
                        (ds6, 1, 1, 200, 50))
            con.execute("INSERT INTO inventory VALUES (?,?,?,?,?)",
                        (ds6, 2, 2, 200, 50))

        con.executemany(
            "INSERT INTO orders (order_id,product_id,order_date,amount,status) "
            "VALUES (?,?,?,?,?)", rows)

        # 评分：6 月女装明显下滑（与退款同源）
        rid = 0
        for d in range(1, 31):
            for _ in range(6):
                rid += 1
                con.execute("INSERT INTO reviews VALUES (?,?,?,?)",
                            (rid, 1, f"2024-05-{d:02d}", 5))
                rid += 1
                con.execute("INSERT INTO reviews VALUES (?,?,?,?)",
                            (rid, 1, f"2024-06-{d:02d}", 3))

        # 退款：6 月男装大量退款（让退款占比上升）
        rf = 0
        for d in range(1, 31):
            for _ in range(2):
                rf += 1
                con.execute("INSERT INTO refunds VALUES (?,?,?,?,?)",
                            (rf, 1, f"2024-06-{d:02d}", 299.0, 1))
        con.commit()
    finally:
        con.close()


def wait(cl, tid, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, st = cl.get(f"/api/tasks/{tid}")
        if isinstance(st, dict) and st.get("task_status") in (
                "success", "failed", "cancelled"):
            return st
        time.sleep(0.4)
    return {"task_status": "timeout"}


def main() -> int:
    print(f"目标服务：{BASE}")
    print(f"待接入库：{DB_PATH}")

    build_my_db(DB_PATH)
    print("✓ 已生成自建库（女装 6 月断崖下滑，男装持平，竞品未降价）")

    adm = C()
    st, _ = adm.post("/auth/login", form={"username": "admin",
                                          "password": "admin123"})
    print(f"管理员登录：{st}")

    # 清理同名旧登记
    _, existing = adm.get("/api/admin/data-sources")
    for d in existing or []:
        if d["name"] == "我的经营库":
            if d["is_active"]:
                print("  跳过清理：它是当前激活库")
                continue
            adm.delete(f"/api/admin/data-sources/{d['id']}")

    # 1. 登记
    s, reg = adm.post("/api/admin/data-sources", data={
        "name": "我的经营库", "path": str(DB_PATH), "read_only": True,
    })
    print(f"\n[1] 登记数据源：HTTP {s}  status={reg.get('status')}  {reg.get('message')}")
    if s != 200:
        return 1
    ds_id = reg["id"]

    # 2. 探查
    s, ins = adm.get(f"/api/admin/data-sources/{ds_id}/inspect")
    print(f"[2] 探查结构：ready={ins.get('ready')}  完整度={ins.get('coverage')}%")
    print(f"    表：{', '.join(ins.get('tables') or [])}")
    if ins.get("missing_required"):
        print(f"    缺表：{ins['missing_required']}")
    if ins.get("impact"):
        for x in ins["impact"][:3]:
            print(f"    影响：{x}")

    # 3. 激活
    s, act = adm.post(f"/api/admin/data-sources/{ds_id}/activate", data={})
    print(f"\n[3] 切换为当前数据源：HTTP {s}  {act.get('message')}")

    # 4. 用新库跑分析
    cl = C()
    cl.post("/auth/login", form={"username": "analyst", "password": "analyst123"})
    _, conv = cl.post("/api/chat/create", data={"title": "自建库归因"})
    cid = conv["conversation_id"]

    _, r = cl.post("/api/chat/send", data={
        "conversation_id": cid,
        "text": "为什么 2024-06 的销售额下滑了？",
    })
    tid = r["task_id"]
    st2 = wait(cl, tid)
    _, res = cl.get(f"/api/results/{tid}")

    print(f"\n[4] 在新库上分析：{st2.get('task_status')}")
    print("    关键指标：")
    for m in res.get("key_metrics") or []:
        print(f"      {m['metric_name']}: {m['metric_value']}{m['metric_unit']}"
              f"（基线 {m.get('baseline_value')}，变动 {m.get('delta_pct')}%）")
    print("    假设：")
    for h in res.get("hypotheses") or []:
        mark = {"成立": "✓", "不成立": "✗", "证据不足": "?"}.get(h["verdict"], "·")
        print(f"      {mark} [{h['verdict']}] {h['statement']}")
        print(f"          {h['reason']}")
    print(f"    结论：{(res.get('conclusion_text') or '')[:220]}")

    # 校验：确实读的是新库（5 月女装 12 单/天 × 30 天 × 299 = 107640 元）
    gmv = next((m["metric_value"] for m in res.get("key_metrics") or []
                if m["metric_name"] == "销售额"), None)
    print(f"\n[5] 校验数据来源：")
    print(f"    6 月销售额 = {gmv}（自建库应为数万元级，示例库是 383 万级）")
    if gmv is not None and gmv < 500000:
        print("    ✓ 确认读的是自建库，不是内置示例库")
    else:
        print("    ✗ 数值仍像示例库，切换可能未生效")
        return 1

    # 清理：切回示例库、删除登记
    _, dss = adm.get("/api/admin/data-sources")
    for d in dss or []:
        if d["id"] == ds_id:
            adm.post(f"/api/admin/data-sources/{ds_id}/activate",
                     data={"force": True})
    print(f"\n[6] 数据源仍处于激活状态（id={ds_id}），"
          f"可在管理页「数据源」里切回或删除。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
