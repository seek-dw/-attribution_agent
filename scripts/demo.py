"""两个业务场景的完整演示链路（FR-11.2 / AC-8）。

场景 A：市场表现分析 —— 销售额下滑归因
场景 B：退款模式分析 —— 退款占比上升归因
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"


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


def wait(cl, tid, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, st = cl.get(f"/api/tasks/{tid}")
        if isinstance(st, dict) and st.get("task_status") in (
                "success", "failed", "cancelled"):
            return st
        time.sleep(0.5)
    return {"task_status": "timeout"}


def run_scenario(name: str, question: str, focus_metric: str) -> bool:
    print("\n" + "=" * 76)
    print(f"场景：{name}")
    print(f"提问：{question}")
    print("=" * 76)

    cl = C()
    cl.post("/auth/login", form={"username": "analyst", "password": "analyst123"})
    _, conv = cl.post("/api/chat/create", data={"title": name})
    cid = conv["conversation_id"]

    # 第一轮：模糊提问 → 澄清
    _, r1 = cl.post("/api/chat/send", data={"conversation_id": cid,
                                            "text": focus_metric})
    wait(cl, r1["task_id"])
    _, res1 = cl.get(f"/api/results/{r1['task_id']}")
    clarified = bool(res1.get("hypotheses") == [] and
                     ("时间范围" in (res1.get("missing_data_text") or "")
                      or "指标" in (res1.get("missing_data_text") or "")))
    print(f"\n[轮次 1] 仅给指标不给范围 → 澄清：{'是' if clarified else '否'}")
    print(f"  {res1.get('conclusion_text', '')[:160]}")

    # 第二轮：完整提问 → 归因
    _, r2 = cl.post("/api/chat/send", data={"conversation_id": cid,
                                            "text": question})
    st = wait(cl, r2["task_id"])
    _, res2 = cl.get(f"/api/results/{r2['task_id']}")

    print(f"\n[轮次 2] 状态：{st.get('task_status')}  步骤：{st.get('step_count')}")
    print("\n关键指标：")
    for m in res2.get("key_metrics", []):
        d = m.get("delta_pct")
        arrow = "" if d is None else (" ↑" if d > 0 else " ↓")
        base = m.get("baseline_value")
        print(f"  · {m['metric_name']}: {m['metric_value']}{m['metric_unit']}"
              f"（基线 {base}，变动 {d}%{arrow}）")
        print(f"    口径：{m['definition']}")

    hyps = res2.get("hypotheses", [])
    print(f"\n假设验证（{len(hyps)} 个）：")
    for h in hyps:
        mark = {"成立": "✓", "不成立": "✗", "证据不足": "?"}.get(h["verdict"], "·")
        print(f"  {mark} {h['statement']}")
        print(f"    条件：{h['criterion']}")
        print(f"    结论：{h['verdict']} —— {h['reason']}")

    print(f"\n归因结论：\n{res2.get('conclusion_text', '')}")
    print(f"\n待补充数据：\n{res2.get('missing_data_text') or '无'}")
    print(f"\n下一步建议：\n{res2.get('next_action_text')}")

    # 导出
    s, body = cl.get(f"/api/results/{r2['task_id']}/download")
    ok_export = (s == 200 and isinstance(body, str)
                 and "经营归因分析报告" in body)
    print(f"\n导出报告：{'成功' if ok_export else '失败'}（{len(body) if isinstance(body, str) else 0} 字符）")

    _, tools_used = cl.get(f"/api/tasks/{r2['task_id']}/tools")
    print(f"工具调用：{len(tools_used)} 次")

    passed = (st.get("task_status") == "success"
              and len(hyps) >= 2
              and ok_export)
    print(f"\n场景结论：{'通过' if passed else '未通过'}")
    return passed


def main() -> int:
    print(f"演示链路 → {BASE}")

    # 前置：服务必须可达
    try:
        probe = C()
        st, _ = probe.get("/auth/login")
        if st != 200:
            print(f"✗ 服务不可达：/auth/login 返回 {st}")
            print("  请先启动服务：uvicorn app.main:app --port 8123")
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 无法连接 {BASE}：{type(exc).__name__}: {exc}")
        print("  请先启动服务，再跑演示。")
        return 2
    a = run_scenario(
        "市场表现分析（销售额下滑归因）",
        "为什么 2024-06 的销售额下滑了？",
        "销售额",
    )
    b = run_scenario(
        "退款模式分析（退款占比上升归因）",
        "为什么 2024-06 的退款占比上升了？",
        "退款",
    )

    print("\n" + "=" * 76)
    print(f"场景 A：{'通过' if a else '未通过'}")
    print(f"场景 B：{'通过' if b else '未通过'}")
    print("=" * 76)
    return 0 if (a and b) else 1


if __name__ == "__main__":
    raise SystemExit(main())
