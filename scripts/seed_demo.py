"""生成演示会话：让页面一打开就有真实分析结果可看。

用法: python scripts/seed_demo.py [base_url]
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

SCENARIOS = [
    {
        "title": "销售额下滑归因 · 6月",
        "attach": ("channel_budget.txt",
                   "2024年6月渠道预算说明：信息流投放预算较5月下调 30%，直播渠道预算持平，"
                   "社群渠道新增两个合作方。竞品在6月未做价格调整，主推仍是同价位段产品。"),
        "turns": ["为什么 2024-06 的销售额下滑了？"],
    },
    {
        "title": "退款占比上升归因 · 6月",
        "attach": None,
        "turns": ["为什么 2024-06 的退款占比上升了？"],
    },
    {
        "title": "成交额取数与缺口演示",
        "attach": None,
        "turns": ["2024-06 的成交额"],
    },
]


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
        time.sleep(0.4)
    return {"task_status": "timeout"}


def upload(cl, cid, name, text):
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="conversation_id"\r\n\r\n{cid}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{text}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/attachment/upload", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with cl.op.open(req, timeout=60) as r:
        return json.loads(r.read())


def main() -> int:
    cl = C()
    st, _ = cl.post("/auth/login", form={"username": "analyst",
                                         "password": "analyst123"})
    print(f"登录：{st}")

    # 幂等：已存在同名会话则跳过，避免重复运行时堆积重复会话
    _, existing = cl.get("/api/chat/ls")
    have = {c["title"] for c in existing} if isinstance(existing, list) else set()

    for sc in SCENARIOS:
        if sc["title"] in have:
            print(f"\n跳过（已存在）：{sc['title']}")
            continue

        _, conv = cl.post("/api/chat/create", data={"title": sc["title"]})
        cid = conv["conversation_id"]
        print(f"\n会话：{sc['title']}（id={cid}）")

        if sc["attach"]:
            name, text = sc["attach"]
            up = upload(cl, cid, name, text)
            print(f"  附件：{up.get('file_name')} → {up.get('parse_status')}")

        for q in sc["turns"]:
            _, r = cl.post("/api/chat/send", data={"conversation_id": cid, "text": q})
            tid = r["task_id"]
            st2 = wait(cl, tid)
            _, res = cl.get(f"/api/results/{tid}")
            hyps = res.get("hypotheses") or []
            print(f"  提问：{q}")
            print(f"    状态={st2.get('task_status')} 假设={len(hyps)} "
                  f"证据={len(res.get('evidence_list') or [])}")
            if hyps:
                for h in hyps:
                    print(f"      [{h['verdict']}] {h['statement']}")
    print("\n演示数据生成完成，刷新页面即可看到。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
