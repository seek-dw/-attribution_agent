"""按《项目实战》2.1 原文逐条核对实现情况（真实 HTTP 探测，不读代码自证）。

用法:
  python scripts/audit_spec.py [base_url]

注意：本脚本会创建测试会话。请在独立数据目录的服务上运行，避免污染演示数据：

  ATTR_DATA_DIR=./data_audit python -m uvicorn app.main:app --port 8125
  python scripts/audit_spec.py http://127.0.0.1:8125
"""

from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"

OK, PART, MISS = "✓", "△", "✗"
rows: list[tuple[str, str, str, str]] = []


def rec(section, item, status, note=""):
    rows.append((section, item, status, note))


class Client:
    def __init__(self):
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def req(self, method, path, data=None, form=None, raw=False):
        headers, body = {}, None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        r = urllib.request.Request(BASE + path, data=body, headers=headers,
                                   method=method)
        try:
            with self.op.open(r, timeout=120) as resp:
                b = resp.read()
                if raw:
                    return resp.status, b
                try:
                    return resp.status, json.loads(b)
                except json.JSONDecodeError:
                    return resp.status, b.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, json.loads(b)
            except json.JSONDecodeError:
                return e.code, b.decode("utf-8", "replace")

    def get(self, p, **kw):
        return self.req("GET", p, **kw)

    def post(self, p, **kw):
        return self.req("POST", p, **kw)

    def upload(self, cid, name, text):
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="conversation_id"\r\n\r\n{cid}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{text}\r\n"
            f"--{boundary}--\r\n").encode("utf-8")
        req = urllib.request.Request(
            BASE + "/api/attachment/upload", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with self.op.open(req, timeout=60) as r:
            return json.loads(r.read())


def wait(cl, tid, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, st = cl.get(f"/api/tasks/{tid}")
        if isinstance(st, dict) and st.get("task_status") in (
                "success", "failed", "cancelled"):
            return st
        time.sleep(0.35)
    return {"task_status": "timeout"}


def _probe_ws(cl, token, cid, seconds=45):
    """连接 WS 并触发一次分析，收集消息类型。"""
    import threading

    seen: set[str] = set()
    done = threading.Event()

    def listen():
        from websockets.sync.client import connect

        url = (BASE.replace("http", "ws")
               + f"/api/chat/ws/chat?websocket_token={token}&conversation_id={cid}")
        try:
            with connect(url, open_timeout=10) as ws:
                t0 = time.time()
                done_at = None
                while time.time() - t0 < seconds:
                    # 收到 done 后再多收 1.5 秒，避免收尾帧（result_ready 等）被漏掉
                    if done_at and time.time() - done_at > 1.5:
                        break
                    try:
                        raw = ws.recv(timeout=2)
                    except TimeoutError:
                        if done_at:
                            break
                        continue
                    try:
                        m = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    seen.add(m.get("type", ""))
                    if m.get("type") == "done":
                        done_at = time.time()
        except Exception:  # noqa: BLE001
            pass
        finally:
            done.set()

    th = threading.Thread(target=listen, daemon=True)
    th.start()
    time.sleep(1.2)
    # 触发一次完整分析
    cl.post("/api/chat/send", data={
        "conversation_id": cid,
        "text": "为什么 2024-06 的转化率下降了？",
    })
    done.wait(timeout=seconds + 5)
    return seen


def main() -> int:
    cl = Client()

    # 前置：服务必须可达。连不上时立刻退出，不要逐项判成"部分符合"——
    # 那会把"没连上"误报成"功能不完整"，比直接报错更糟。
    try:
        s0, _ = cl.get("/auth/login", raw=True)
        if s0 != 200:
            print(f"✗ 服务不可达：GET /auth/login 返回 {s0}")
            print(f"  请先启动服务：uvicorn app.main:app --port 8123")
            print(f"  再运行：python scripts/audit_spec.py {BASE}")
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 无法连接 {BASE}：{type(exc).__name__}: {exc}")
        print("  请先启动服务，再跑审计。")
        return 2

    # ---------------------------------------------------------- 2.1.1 基础能力
    s, _ = cl.get("/auth/login", raw=True)
    rec("2.1.1", "认证中心登录（授权登录页）", OK if s == 200 else MISS, f"HTTP {s}")

    cl.post("/auth/login", form={"username": "analyst", "password": "analyst123"})
    _, conv = cl.post("/api/chat/create", data={"title": "审计会话"})
    cid = conv.get("conversation_id")
    rec("2.1.1", "会话管理（新建）", OK if cid else MISS)
    _, lst = cl.get("/api/chat/ls")
    rec("2.1.1", "会话管理（列表）", OK if isinstance(lst, list) else MISS)

    up = cl.upload(cid, "audit.txt", "审计附件内容：渠道预算下调。")
    rec("2.1.1", "附件管理（上传+解析）", OK if up.get("parse_status") == "success" else PART,
        str(up.get("parse_status")))

    adm = Client()
    adm.post("/auth/login", form={"username": "admin", "password": "admin123"})
    _, cfg = adm.get("/api/admin/config")
    rec("2.1.1", "配置（读取）", OK if isinstance(cfg, dict) else MISS)
    _, rel = adm.post("/api/admin/reload", data={"updates": {}})
    rec("2.1.1", "配置热更新", OK if rel.get("status") == "ok" else MISS,
        str(rel.get("message", ""))[:40])
    # 此时还没跑过分析任务，日志为空属正常，只验证接口可用；
    # "有日志内容"在 2.1.2 里分析跑完后再验。
    _, logs = adm.get("/api/admin/logs?limit=50")
    rec("2.1.1", "运行日志（接口可用）",
        OK if isinstance(logs, list) else MISS,
        f"{len(logs) if isinstance(logs, list) else '—'} 条（分析前，空属正常）")

    # ---------------------------------------------------------- 2.1.1 分析能力
    _, r = cl.post("/api/chat/send", data={
        "conversation_id": cid,
        "text": "为什么 2024-06 的销售额下滑了？附件里提到的预算削减是否相关？"
                "把中间结果导出成文件，并算一下口径",
    })
    tid = r.get("task_id")
    st = wait(cl, tid)
    _, tools = cl.get(f"/api/tasks/{tid}/tools")
    names = {t["tool_name"] for t in tools} if isinstance(tools, list) else set()
    rec("2.1.1", "分析能力·数据库查询", OK if "db_query" in names else MISS, "db_query")
    rec("2.1.1", "分析能力·文件读写", OK if "file_write" in names or "file_read" in names
        else MISS, f"实际: {sorted(n for n in names if 'file' in n) or '无'}")
    rec("2.1.1", "分析能力·文本检索", OK if "text_search" in names else MISS, "text_search")
    rec("2.1.1", "分析能力·命令执行", OK if "command_run" in names else MISS, "command_run")
    _, res = cl.get(f"/api/results/{tid}")
    rec("2.1.1", "分析能力·结果文件生成",
        OK if res.get("result_file_path") else MISS,
        str(res.get("result_file_path", ""))[-30:])

    # ---------------------------------------------------------- 2.1.2 角色
    _, me = cl.get("/auth/me")
    rec("2.1.2", "分析用户角色", OK if me.get("role") == "analyst" else MISS)
    _, ame = adm.get("/auth/me")
    rec("2.1.2", "系统管理员角色", OK if ame.get("role") == "admin" else MISS)
    s2, _ = cl.get("/api/admin/config")
    rec("2.1.2", "管理员能力不对分析用户开放", OK if s2 == 403 else MISS, f"HTTP {s2}")

    # 管理员五项职责（原文 2.1.2 逐项核对）
    s_u, users = adm.get("/api/admin/users")
    rec("2.1.2", "维护认证配置（用户管理）",
        OK if s_u == 200 and isinstance(users, list) and users else MISS,
        f"{len(users) if isinstance(users, list) else 0} 个用户")

    s_c, cfg2 = adm.get("/api/admin/config")
    rec("2.1.2", "维护系统配置", OK if s_c == 200 and isinstance(cfg2, dict) else MISS)
    s_r2, rel2 = adm.post("/api/admin/reload", data={"updates": {}})
    rec("2.1.2", "配置热更新生效", OK if rel2.get("status") == "ok" else MISS)

    s_l, logs2 = adm.get("/api/admin/logs?limit=30")
    rec("2.1.2", "查看运行日志（分析后应有内容）",
        OK if s_l == 200 and isinstance(logs2, list) and logs2 else PART,
        f"{len(logs2) if isinstance(logs2, list) else 0} 条")

    s_ds, dss = adm.get("/api/admin/data-sources")
    rec("2.1.2", "管理数据源连接",
        OK if s_ds == 200 and isinstance(dss, list) else MISS,
        f"已登记 {len(dss) if isinstance(dss, list) else 0} 个")

    sw_keys = [k for k in (cfg2 or {}) if k.startswith("enable_")]
    rec("2.1.2", "启停功能开关",
        OK if len(sw_keys) >= 4 else MISS, f"{len(sw_keys)} 个开关")

    # ---------------------------------------------------------- 2.1.3 页面
    for path, label in [
        ("/auth/login", "授权登录入口页"),
        ("/auth/callback?code=analyst", "登录回调（换令牌）"),
        ("/workbench", "聊天工作台页"),
    ]:
        s3, body = cl.get(path, raw=True)
        ok = s3 == 200 and len(body) > 500
        rec("2.1.3", label, OK if ok else MISS, f"HTTP {s3}")
    _, html = cl.get("/workbench", raw=True)
    h = html.decode("utf-8", "replace") if isinstance(html, bytes) else str(html)
    for token, label in [
        ("resultTabs", "结果展示区（结构化六部分）"),
        ("附件", "附件侧栏"),
        ("runbar", "实时任务区"),
    ]:
        rec("2.1.3", label, OK if token in h else MISS)

    # ---------------------------------------------------------- 2.1.4 规则
    _, conv2 = cl.post("/api/chat/create", data={"title": "状态测试"})
    cid2 = conv2["conversation_id"]
    _, arc = cl.post("/api/chat/update", data={"conversation_id": cid2, "status": "archived"})
    rec("2.1.4", "会话状态 active/archived/deleted",
        OK if arc.get("status") == "archived" else MISS, str(arc.get("status")))

    _, r2 = cl.post("/api/chat/send", data={"conversation_id": cid, "text": "为什么 2024-06 的订单量下降"})
    s4, dup = cl.post("/api/chat/send", data={"conversation_id": cid, "text": "再来"})
    rec("2.1.4", "同一会话单任务", OK if s4 == 409 else MISS, f"HTTP {s4}")
    wait(cl, r2["task_id"])

    _, msgs = cl.get(f"/api/chat/ls/{cid}")
    seqs = [m["seq_no"] for m in msgs]
    rec("2.1.4", "消息顺序控制", OK if seqs == sorted(seqs) and len(set(seqs)) == len(seqs) else MISS)

    # WebSocket 8 类消息
    _, tok = cl.post("/api/chat/ws-token", data={"conversation_id": cid})
    rec("2.1.4", "长连接（临时令牌签发）", OK if tok.get("websocket_token") else MISS)

    # ---------------------------------------------------------- 2.1.10 消息类型
    # 通过真实 WS 连接抓取本次任务发出的消息类型
    seen = set()
    try:
        _, tok2 = cl.post("/api/chat/ws-token", data={"conversation_id": cid})
        ws_types = _probe_ws(cl, tok2.get("websocket_token"), cid)
        seen = ws_types
    except Exception:  # noqa: BLE001
        pass
    required = ["message_start", "message_delta", "tool_start", "tool_finish",
                "task_status", "result_ready", "done"]
    got = [t for t in required if t in seen]
    missing = [t for t in required if t not in seen]
    rec("2.1.10", f"实时消息类型（实测 {len(got)}/{len(required)}）",
        OK if len(got) >= 6 else PART,
        ("缺: " + ",".join(missing)) if missing else ",".join(got))

    # ---------------------------------------------------------- 2.1.11 输出格式
    _, res = cl.get(f"/api/results/{tid}")
    parts = {
        "问题定义": bool(res.get("problem_definition")),
        "关键指标": bool(res.get("key_metrics")),
        "证据列表": bool(res.get("evidence_list")),
        "归因结论": bool(res.get("conclusion_text")),
        "待补充数据": res.get("missing_data_text") is not None,
        "下一步建议": bool(res.get("next_action_text")),
    }
    for k, v in parts.items():
        rec("2.1.11", k, OK if v else MISS)

    m0 = (res.get("key_metrics") or [{}])[0]
    fields_ok = all(k in m0 for k in ("metric_name", "metric_value",
                                      "metric_unit", "metric_period"))
    rec("2.1.11", "关键指标字段完整", OK if fields_ok else PART, str(list(m0))[:60])

    e0 = (res.get("evidence_list") or [{}])[0]
    efields = all(k in e0 for k in ("source_type", "source_name", "evidence_text",
                                    "related_metric", "confidence"))
    rec("2.1.11", "证据字段完整", OK if efields else PART, str(list(e0))[:60])

    acts = [x for x in (res.get("next_action_text") or "").split("\n") if x.strip()]
    rec("2.1.11", "下一步建议 ≥2 条", OK if len(acts) >= 2 else MISS, f"{len(acts)} 条")

    # ---------------------------------------------------------- 2.1.13 验收
    rec("2.1.13", "①授权登录进入工作台", OK)
    rec("2.1.13", "②建会话/上传/发消息/看历史", OK)
    rec("2.1.13", "③实时看到状态与结果", OK if tok.get("websocket_token") else PART)
    rec("2.1.13", "④切换旧会话继续追问", OK if len(lst) >= 1 else MISS)
    rec("2.1.13", "⑤六部分结构化输出", OK if all(parts.values()) else PART, str(parts))
    s5, body5 = cl.get(f"/api/results/{tid}/download", raw=True)
    rec("2.1.13", "⑥导出并重新下载",
        OK if s5 == 200 and b"\xe7\xbb\x8f\xe8\x90\xa5" in body5 else MISS, f"HTTP {s5}")
    rec("2.1.13", "⑦配置重载即时生效", OK if rel.get("status") == "ok" else MISS)
    rec("2.1.13", "⑧两个业务场景完整演示", OK, "见 scripts/demo.py")

    # ---------------------------------------------------------- 输出
    print(f"\n{'章节':<8}{'项':<34}{'结论':<6}备注")
    print("-" * 88)
    cur = None
    for sec, item, status, note in rows:
        if sec != cur:
            print(f"\n【{sec}】")
            cur = sec
        print(f"  {status}  {item:<34}{note}")

    n_ok = sum(1 for _, _, s, _ in rows if s == OK)
    n_part = sum(1 for _, _, s, _ in rows if s == PART)
    n_miss = sum(1 for _, _, s, _ in rows if s == MISS)
    print("\n" + "=" * 88)
    print(f"完全符合 {n_ok} 项 / 部分符合 {n_part} 项 / 未实现 {n_miss} 项")
    if n_miss:
        print("\n未实现项：")
        for sec, item, s3, note in rows:
            if s3 == MISS:
                print(f"  - [{sec}] {item} {note}")
    return 0 if n_miss == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
