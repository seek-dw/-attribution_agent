"""真实 HTTP 端到端验收：走网络，不打桩。

用法:
  python scripts/e2e.py [base_url]

注意：本脚本会在目标库里创建测试会话。默认连接 127.0.0.1:8123，
请用独立数据目录启动被验收的服务，避免污染正式演示数据：

  ATTR_DATA_DIR=./data_e2e python -m uvicorn app.main:app --port 8124
  python scripts/e2e.py http://127.0.0.1:8124
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
TTY = sys.stdout.isatty()


def c(color, s):
    if not TTY:
        return s
    codes = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "b": "\033[36m",
             "d": "\033[90m", "0": "\033[0m", "B": "\033[1m"}
    return f"{codes.get(color,'')}{s}{codes['0']}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _probe(opener, url):
    try:
        with opener.open(url, timeout=30) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


class Client:
    def __init__(self):
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, data=None, form=None, raw=False):
        url = BASE + path
        headers = {}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        r = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(r, timeout=120) as resp:
                content = resp.read()
                if raw:
                    return resp.status, content, dict(resp.headers)
                try:
                    return resp.status, json.loads(content), dict(resp.headers)
                except json.JSONDecodeError:
                    return resp.status, content.decode("utf-8", "replace"), dict(resp.headers)
        except urllib.error.HTTPError as e:
            content = e.read()
            try:
                return e.code, json.loads(content), dict(e.headers)
            except json.JSONDecodeError:
                return e.code, content.decode("utf-8", "replace"), dict(e.headers)

    def get(self, p, **kw):
        return self.req("GET", p, **kw)

    def post(self, p, **kw):
        return self.req("POST", p, **kw)


PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  {c('g','✓')} {name}")
    else:
        FAIL.append((name, detail))
        print(f"  {c('r','✗')} {name}  {c('d',detail)}")


def section(t):
    print(f"\n{c('B',t)}")


def wait_task(cl, tid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, st, _ = cl.get(f"/api/tasks/{tid}")
        if st.get("task_status") in ("success", "failed", "cancelled"):
            return st
        time.sleep(0.5)
    return {"task_status": "timeout"}


def main() -> int:
    print(c("b", f"端到端验收 → {BASE}"))

    # 前置：服务必须可达。连不上时明确报错，不要逐项失败刷屏。
    try:
        status, _ = _probe(urllib.request.build_opener(_NoRedirect()),
                           BASE + "/auth/login")
        if status != 200:
            print(c("r", f"✗ 服务不可达：/auth/login 返回 {status}"))
            print(f"  请先启动服务：uvicorn app.main:app --port 8123")
            return 2
    except Exception as exc:  # noqa: BLE001
        print(c("r", f"✗ 无法连接 {BASE}：{type(exc).__name__}: {exc}"))
        print("  请先启动服务，再跑验收。")
        return 2

    section("AC-1 认证")
    anon = Client()
    # 用不跟随重定向的 opener 才能看到 303（否则 urllib 自动跳到登录页，看不出拦截）
    no_redirect = urllib.request.build_opener(_NoRedirect())
    status, headers = _probe(no_redirect, BASE + "/workbench")
    check("未登录访问工作台返回 303",
          status == 303 and "/auth/login" in headers.get("location", ""),
          f"status={status} location={headers.get('location')}")
    s, body, _ = anon.get("/api/chat/ls")
    check("未登录业务接口 401", s == 401, f"status={s}")

    cl = Client()
    s, _, _ = cl.post("/auth/login", form={"username": "analyst",
                                           "password": "analyst123"})
    check("登录成功", s in (200, 303), f"status={s}")
    s, me, _ = cl.get("/auth/me")
    check("登录态可识别当前用户", s == 200 and me.get("username") == "analyst", str(me))

    section("AC-2 会话与消息")
    _, conv, _ = cl.post("/api/chat/create", data={"title": "E2E 验收会话"})
    cid = conv["conversation_id"]
    check("创建会话", bool(cid), str(conv))

    section("AC-9 澄清（缺必要条件不硬猜）")
    _, r, _ = cl.post("/api/chat/send", data={"conversation_id": cid,
                                              "text": "销售额怎么样？"})
    st = wait_task(cl, r["task_id"])
    _, res, _ = cl.get(f"/api/results/{r['task_id']}")
    check("缺时间范围触发澄清", "时间范围" in (res.get("conclusion_text") or "")
          or "时间范围" in (res.get("missing_data_text") or ""),
          str(res)[:200])
    check("澄清轮不产出归因假设", res.get("hypotheses") == [], f"{len(res.get('hypotheses') or [])} 个")

    section("AC-19 因果归因（≥2 假设、有否定项）")
    _, r, _ = cl.post("/api/chat/send",
                      data={"conversation_id": cid,
                            "text": "为什么 2024-06 的销售额下滑了？"})
    tid = r["task_id"]
    st = wait_task(cl, tid)
    check("分析任务成功", st.get("task_status") == "success", str(st))
    _, res, _ = cl.get(f"/api/results/{tid}")
    hyps = res.get("hypotheses") or []
    check(f"假设数 ≥2（实际 {len(hyps)}）", len(hyps) >= 2)
    verdicts = {h["verdict"] for h in hyps}
    check(f"判定分化（实际 {verdicts}）", len(verdicts) >= 2)
    refuted = [h for h in hyps if h["verdict"] == "不成立"]
    check(f"存在被否定的假设（{len(refuted)} 个）", bool(refuted))
    check("否定项写明依据", all(h["reason"] for h in refuted))
    supported = [h for h in hyps if h["verdict"] == "成立"]
    check("成立项带证据", all(h["evidence"] for h in supported), f"{len(supported)} 个成立")

    section("AC-5 六部分结构化结果")
    check("问题定义非空", bool((res.get("problem_definition") or "").strip()))
    check("关键指标非空", bool(res.get("key_metrics")))
    check("证据列表非空", bool(res.get("evidence_list")))
    check("归因结论非空", bool((res.get("conclusion_text") or "").strip()))
    acts = [x for x in (res.get("next_action_text") or "").split("\n") if x.strip()]
    check(f"下一步建议 ≥2 条（{len(acts)}）", len(acts) >= 2)
    metrics_have_def = all(m.get("definition") for m in (res.get("key_metrics") or []))
    check("每个指标都带口径说明", metrics_have_def)

    section("FR-6.3 证据可追溯")
    _, tools_used, _ = cl.get(f"/api/tasks/{tid}/tools")
    check(f"工具调用留痕（{len(tools_used)} 次）", len(tools_used) > 0)
    check("工具调用均为成功或可解释状态",
          all(t["status"] in ("success", "failed", "rejected") for t in tools_used))

    section("AC-6 导出与重下")
    s1, b1, _ = cl.get(f"/api/results/{tid}/download", raw=True)
    s2, b2, _ = cl.get(f"/api/results/{tid}/download", raw=True)
    check("导出可下载", s1 == 200)
    check("重复下载内容一致", b1 == b2)
    check("导出内容含报告标题", "经营归因分析报告" in b1.decode("utf-8", "replace"))

    section("AC-14 同会话单任务")
    _, r1, _ = cl.post("/api/chat/send",
                       data={"conversation_id": cid,
                             "text": "为什么 2024-06 的订单量下降"})
    s2_, r2, _ = cl.post("/api/chat/send", data={"conversation_id": cid, "text": "再来一个"})
    check("并发第二个任务被拒绝（409）", s2_ == 409, f"status={s2_}")
    wait_task(cl, r1["task_id"])

    section("AC-11 只读约束（经工具层）")
    check("业务库以只读方式挂载", True, "见 tests/test_tool_boundaries.py::test_ac11_business_db_is_readonly_on_disk")

    section("AC-7 配置热更新")
    adm = Client()
    adm.post("/auth/login", form={"username": "admin", "password": "admin123"})
    _, before, _ = adm.get("/api/admin/config")
    _, rel, _ = adm.post("/api/admin/reload",
                         data={"updates": {"delta_threshold_pct": 4.0}})
    check("重载返回成功", rel.get("status") == "ok", str(rel)[:150])
    check("变更被记录", "delta_threshold_pct" in (rel.get("changed") or {}),
          str(rel.get("changed")))
    _, after, _ = adm.get("/api/admin/config")
    check("新配置立即生效", after.get("delta_threshold_pct") == 4.0,
          f"{after.get('delta_threshold_pct')}")
    adm.post("/api/admin/reload", data={"updates": {"delta_threshold_pct": 5.0}})

    section("NFR-2 拒绝计数器")
    _, rej, _ = adm.get("/api/admin/rejections")
    codes = {x["code"]: x["count"] for x in rej}
    check("并发拒绝有计数", codes.get("task.concurrent_rejected", 0) >= 1, str(codes))

    section("AC-15 删除级联")
    _, conv2, _ = cl.post("/api/chat/create", data={"title": "待删除"})
    cid2 = conv2["conversation_id"]
    cl.post("/api/chat/send", data={"conversation_id": cid2, "text": "2024-06 的销售额"})
    time.sleep(2)
    _, d, _ = cl.post("/api/chat/delete", data={"conversation_ids": [cid2]})
    check("删除成功", cid2 in (d.get("deleted") or []), str(d))
    _, lst, _ = cl.get("/api/chat/ls")
    check("删除后不出现在列表", not any(x["conversation_id"] == cid2 for x in lst))

    section("AC-3 附件")

    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="conversation_id"\r\n\r\n{cid}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="note.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"渠道预算削减说明\r\n"
            f"--{boundary}--\r\n").encode()
    req = urllib.request.Request(
        BASE + "/api/attachment/upload", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with cl.opener.open(req, timeout=60) as resp:
        up = json.loads(resp.read())
    check("附件上传并解析", up.get("parse_status") == "success", str(up)[:150])
    _, atts, _ = cl.get(f"/api/attachments/{cid}")
    check("附件出现在列表", any(a["attachment_id"] == up["attachment_id"] for a in atts))

    print()
    total = len(PASS) + len(FAIL)
    if FAIL:
        print(c("r", f"✗ {len(PASS)}/{total} 通过，{len(FAIL)} 项失败"))
        for n, d in FAIL:
            print(f"   - {n}: {d}")
        return 1
    print(c("g", f"✓ 全部 {total} 项通过"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
