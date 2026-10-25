"""HTTP + WebSocket 接口层。"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, UploadFile,
    WebSocket, WebSocketDisconnect, status,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import config
from . import datasource as ds
from .analysis import runner
from .analysis.engine import parse_question
from .auth.session import (
    COOKIE_NAME, current_user, issue_session, parse_session, require_admin,
    verify_password,
)
from .db import (
    AnalysisResult, AnalysisTask, Attachment, Conversation, DataSource, Message,
    RejectionCounter, SessionState, ToolInvocation, User, WebsocketToken,
    session_dep,
)

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "web" / "templates"))


# --------------------------------------------------------------------------
# 实时连接管理
# --------------------------------------------------------------------------
class Hub:
    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, conversation_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.setdefault(conversation_id, set()).add(q)
        return q

    def unsubscribe(self, conversation_id: int, q: asyncio.Queue) -> None:
        self._subs.get(conversation_id, set()).discard(q)

    def publish(self, conversation_id: int, kind: str, payload: dict[str, Any]) -> None:
        msg = {"type": kind, **payload}
        for q in list(self._subs.get(conversation_id, ())):
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._safe_put, q, msg)
            else:
                self._safe_put(q, msg)

    @staticmethod
    def _safe_put(q: asyncio.Queue, msg: dict[str, Any]) -> None:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


hub = Hub()
router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# 认证（FR-1.1 ~ FR-1.3）
# --------------------------------------------------------------------------
@router.get("/auth/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    """FR-1.1：入口页只展示登录动作，不展示业务数据。"""
    return templates.TemplateResponse(request, "login.html")


@router.post("/auth/login")
def login_submit(username: str = Form(...), password: str = Form(...),
                 s: Session = Depends(session_dep)):
    user = s.query(User).filter_by(username=username).one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return JSONResponse({"detail": "用户名或密码错误"}, status_code=401)
    # 停用状态必须在登录时拦住，不能只靠后续接口的 current_user 校验
    if user.status != "active":
        return JSONResponse({"detail": "该账号已停用，请联系管理员"}, status_code=401)
    # FR-1.2：回调换令牌 → 建立登录态 → 跳转工作台
    token = issue_session(user.id)
    resp = RedirectResponse("/workbench", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                    max_age=int(timedelta(hours=12).total_seconds()))
    return resp


@router.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "",
                  s: Session = Depends(session_dep)):
    """模拟认证中心回调：code 即用户名占位，便于演示授权跳转形态。"""
    if not code:
        return RedirectResponse("/auth/login", status_code=303)
    user = s.query(User).filter_by(username=code).one_or_none()
    if user is None or user.status != "active":
        return RedirectResponse("/auth/login", status_code=303)
    token = issue_session(user.id)
    resp = RedirectResponse("/workbench", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                    max_age=int(timedelta(hours=12).total_seconds()))
    return resp


@router.get("/auth/logout")
def logout():
    resp = RedirectResponse("/auth/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username,
            "display_name": user.display_name, "role": user.role}


# --------------------------------------------------------------------------
# 页面（FR-1.3：未登录跳回入口）
# --------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def index(request: Request, s: Session = Depends(session_dep)):
    if parse_session(request.cookies.get(COOKIE_NAME)) is None:
        return RedirectResponse("/auth/login", status_code=303)
    return RedirectResponse("/workbench", status_code=303)


@router.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request, s: Session = Depends(session_dep)):
    uid = parse_session(request.cookies.get(COOKIE_NAME))
    if uid is None:
        return RedirectResponse("/auth/login", status_code=303)
    user = s.get(User, uid)
    if user is None:
        return RedirectResponse("/auth/login", status_code=303)
    return templates.TemplateResponse(
        request, "workbench.html", {"user": user})


# --------------------------------------------------------------------------
# 会话（FR-2.1 ~ FR-2.7）
# --------------------------------------------------------------------------
def _own_conv(s: Session, user: User, cid: int) -> Conversation:
    conv = s.get(Conversation, cid)
    if conv is None or conv.user_id != user.id or conv.status == "deleted":
        raise HTTPException(404, "会话不存在")
    return conv


@router.post("/api/chat/create")
def chat_create(payload: dict | None = None, user: User = Depends(current_user),
                s: Session = Depends(session_dep)):
    title = (payload or {}).get("title") or "新会话"
    conv = Conversation(user_id=user.id, title=title[:200])
    s.add(conv)
    s.commit()
    return {"conversation_id": conv.id, "title": conv.title, "status": conv.status}


@router.get("/api/chat/ls")
def chat_ls(user: User = Depends(current_user), s: Session = Depends(session_dep)):
    rows = (s.query(Conversation)
            .filter(Conversation.user_id == user.id,
                    Conversation.status != "deleted")
            .order_by(Conversation.updated_at.desc()).all())
    return [{
        "conversation_id": c.id, "title": c.title, "status": c.status,
        "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
    } for c in rows]


@router.post("/api/chat/update")
def chat_update(payload: dict, user: User = Depends(current_user),
                s: Session = Depends(session_dep)):
    conv = _own_conv(s, user, int(payload["conversation_id"]))
    if payload.get("title") is not None:
        conv.title = str(payload["title"])[:200]
    # 2.1.4 要求会话状态含 active / archived / deleted
    if payload.get("status") is not None:
        new_status = str(payload["status"])
        if new_status not in ("active", "archived", "deleted"):
            raise HTTPException(400, "状态只能是 active / archived / deleted")
        if new_status == "deleted":
            raise HTTPException(400, "归档请用 archived；删除请调用 /api/chat/delete")
        conv.status = new_status
    conv.updated_at = _now()
    s.commit()
    return {"conversation_id": conv.id, "title": conv.title, "status": conv.status}


@router.post("/api/chat/delete")
def chat_delete(payload: dict, user: User = Depends(current_user),
                s: Session = Depends(session_dep)):
    deleted = []
    for cid in payload.get("conversation_ids", []):
        conv = s.get(Conversation, int(cid))
        if conv is None or conv.user_id != user.id:
            continue
        runner.delete_conversation_cascade(s, conv)
        deleted.append(int(cid))
    s.commit()
    return {"deleted": deleted}


@router.get("/api/chat/ls/{conversation_id}")
def chat_messages(conversation_id: int, user: User = Depends(current_user),
                  s: Session = Depends(session_dep)):
    conv = _own_conv(s, user, conversation_id)
    msgs = (s.query(Message).filter_by(conversation_id=conv.id)
            .order_by(Message.seq_no).all())
    return [{
        "message_id": m.id, "role": m.role, "content": m.content,
        "message_type": m.message_type, "tool_name": m.tool_name,
        "seq_no": m.seq_no,
        "attachments": [
            {"attachment_id": a.id, "file_name": a.file_name}
            for a in s.query(Attachment).filter_by(message_id=m.id).all()
        ],
        "created_at": m.created_at.isoformat(),
    } for m in msgs]


@router.post("/api/chat/send")
def chat_send(payload: dict, user: User = Depends(current_user),
              s: Session = Depends(session_dep)):
    conv = _own_conv(s, user, int(payload["conversation_id"]))
    text_in = str(payload.get("text", "")).strip()
    if not text_in:
        raise HTTPException(400, "消息内容为空")

    # FR-2.4：同一会话单任务，由 DB 部分唯一索引 + 预检查双保险
    running = (s.query(AnalysisTask)
               .filter_by(conversation_id=conv.id, task_status="running").first())
    if running is not None:
        from .db import bump_rejection
        bump_rejection(s, "task.concurrent_rejected")
        s.commit()
        return JSONResponse(
            {"detail": "该会话已有分析任务在执行，请等待完成或取消",
             "running_task_id": running.id}, status_code=409)

    runner.add_message(s, conv.id, "user", text_in)
    task = AnalysisTask(conversation_id=conv.id, user_id=user.id,
                        input_text=text_in, task_status="queued")
    s.add(task)
    try:
        s.commit()
    except Exception:
        s.rollback()
        from .db import bump_rejection
        bump_rejection(s, "task.concurrent_rejected")
        s.commit()
        return JSONResponse({"detail": "并发冲突：该会话已有任务在执行"}, status_code=409)

    _spawn(s, task.id, conv.id)
    return {"task_id": task.id, "conversation_id": conv.id, "task_status": "queued"}


def _spawn(prev: Session, task_id: int, conversation_id: int) -> None:
    import threading

    from .db import get_session as new_session

    def worker() -> None:
        s2 = new_session()
        try:
            task = s2.get(AnalysisTask, task_id)
            if task is None:
                return
            emit = lambda kind, payload: hub.publish(conversation_id, kind, payload)  # noqa: E731
            runner.run_task(s2, task, emit)
            # 功能开关：关闭时不生成摘要（2.1.2 启停功能开关）
            if config.switch_on("enable_context_summary"):
                runner.maybe_summarize(s2, conversation_id)
            s2.commit()
        except Exception as exc:  # noqa: BLE001
            s2.rollback()
            task = s2.get(AnalysisTask, task_id)
            if task is not None:
                task.task_status = "failed"
                task.error_message = str(exc)[:1000]
                runner.log(s2, task_id, conversation_id, "error", "task",
                           f"后台任务异常: {type(exc).__name__}: {exc}")
                s2.commit()
            hub.publish(conversation_id, "error",
                        {"task_id": task_id, "error_message": str(exc)[:500]})
        finally:
            s2.close()

    threading.Thread(target=worker, daemon=True).start()


@router.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: int, user: User = Depends(current_user),
                s: Session = Depends(session_dep)):
    task = s.get(AnalysisTask, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "任务不存在")
    if task.task_status not in ("queued", "running"):
        return {"task_id": task_id, "task_status": task.task_status,
                "note": "任务已结束，无需取消"}
    task.task_status = "cancelled"
    task.finished_at = _now()
    task.current_step = "已取消"
    runner.log(s, task_id, task.conversation_id, "warning", "task", "任务被用户取消")
    s.commit()
    hub.publish(task.conversation_id, "task_status",
                {"task_id": task_id, "task_status": "cancelled", "current_step": "已取消"})
    return {"task_id": task_id, "task_status": "cancelled"}


@router.get("/api/tasks/{task_id}")
def get_task(task_id: int, user: User = Depends(current_user),
             s: Session = Depends(session_dep)):
    task = s.get(AnalysisTask, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": task.id, "task_status": task.task_status,
        "current_step": task.current_step, "step_count": task.step_count,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "error_message": task.error_message,
    }


@router.get("/api/results/{task_id}")
def get_result(task_id: int, user: User = Depends(current_user),
               s: Session = Depends(session_dep)):
    task = s.get(AnalysisTask, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "任务不存在")
    res = s.query(AnalysisResult).filter_by(task_id=task_id).one_or_none()
    if res is None:
        raise HTTPException(404, "结果尚未生成")
    return _result_payload(task, res)


def _result_payload(task: AnalysisTask, res: AnalysisResult) -> dict[str, Any]:
    """统一结果载荷，避免多处拼装导致字段漏传。"""
    return {
        "task_id": task.id,
        "result_id": res.id,
        "problem_definition": res.problem_definition,
        "key_metrics": json.loads(res.key_metrics_json or "[]"),
        "evidence_list": json.loads(res.evidence_list_json or "[]"),
        "conclusion_text": res.conclusion_text,
        "missing_data_text": res.missing_data_text,
        "next_action_text": res.next_action_text,
        "hypotheses": json.loads(res.hypotheses_json or "[]"),
        "tool_plan": json.loads(getattr(res, "tool_plan_json", "{}") or "{}"),
        "artifacts": json.loads(getattr(res, "artifacts_json", "[]") or "[]"),
        "degraded_note": getattr(res, "degraded_note", "") or "",
        "result_file_path": res.result_file_path,
    }


@router.get("/api/chat/last-result/{conversation_id}")
def last_result(conversation_id: int, user: User = Depends(current_user),
                s: Session = Depends(session_dep)):
    """切回旧会话时恢复最近一次分析结果（FR-2.7 历史回放）。"""
    conv = _own_conv(s, user, conversation_id)
    task = (s.query(AnalysisTask)
            .filter(AnalysisTask.conversation_id == conv.id,
                    AnalysisTask.task_status == "success")
            .order_by(AnalysisTask.id.desc()).first())
    if task is None:
        raise HTTPException(404, "该会话暂无分析结果")
    res = s.query(AnalysisResult).filter_by(task_id=task.id).one_or_none()
    if res is None:
        raise HTTPException(404, "结果尚未生成")
    return _result_payload(task, res)


@router.get("/api/results/{task_id}/download")
def download_result(task_id: int, user: User = Depends(current_user),
                    s: Session = Depends(session_dep)):
    if not config.switch_on("enable_export"):
        raise HTTPException(403, "导出功能已被管理员关闭")
    task = s.get(AnalysisTask, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "任务不存在")
    res = s.query(AnalysisResult).filter_by(task_id=task_id).one_or_none()
    if res is None or not res.result_file_path:
        raise HTTPException(404, "结果文件不存在")
    p = Path(res.result_file_path)
    if not p.exists():
        raise HTTPException(404, "结果文件已丢失")
    return FileResponse(p, filename=p.name, media_type="text/markdown")


# --------------------------------------------------------------------------
# 附件（FR-3.1 ~ FR-3.6）
# --------------------------------------------------------------------------
@router.post("/api/attachment/upload")
async def upload(conversation_id: int = Form(...), file: UploadFile = File(...),
                 user: User = Depends(current_user), s: Session = Depends(session_dep)):
    if not config.switch_on("enable_attachment"):
        raise HTTPException(403, "附件功能已被管理员关闭")
    conv = _own_conv(s, user, conversation_id)
    base = config.STORAGE_DIR / "uploads" / str(user.id) / str(conv.id)
    base.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "upload.bin").name
    target = (base / safe_name).resolve()
    if base.resolve() not in target.parents and target.parent != base.resolve():
        raise HTTPException(400, "文件名非法")

    data = await file.read()
    target.write_bytes(data)

    parse_status, parse_error, excerpt = "success", "", ""
    try:
        suffix = target.suffix.lower()
        if suffix in (".txt", ".md", ".csv", ".json", ".log"):
            excerpt = data.decode("utf-8", errors="replace")[:20000]
        elif suffix == ".pdf":
            excerpt = ""
            parse_error = "PDF 文本抽取未启用；本次仅登记文件，不参与证据"
            parse_status = "failed"
        else:
            excerpt = data.decode("utf-8", errors="replace")[:20000]
    except Exception as exc:  # noqa: BLE001
        parse_status, parse_error = "failed", f"{type(exc).__name__}: {exc}"

    att = Attachment(
        conversation_id=conv.id, file_name=safe_name, file_path=str(target),
        file_type=target.suffix.lstrip(".").lower(), file_size=len(data),
        parse_status=parse_status, parse_error=parse_error, text_excerpt=excerpt,
    )
    s.add(att)
    if parse_status == "failed":
        runner.log(s, None, conv.id, "warning", "attachment",
                   f"附件 {safe_name} 解析失败：{parse_error}")
    s.commit()
    return {"attachment_id": att.id, "file_name": att.file_name,
            "file_path": att.file_path, "parse_status": att.parse_status,
            "parse_error": att.parse_error}


@router.get("/api/attachments/{conversation_id}")
def list_attachments(conversation_id: int, user: User = Depends(current_user),
                     s: Session = Depends(session_dep)):
    conv = _own_conv(s, user, conversation_id)
    rows = (s.query(Attachment).filter_by(conversation_id=conv.id)
            .order_by(Attachment.id.desc()).all())
    return [{
        "attachment_id": a.id, "file_name": a.file_name, "file_type": a.file_type,
        "file_size": a.file_size, "parse_status": a.parse_status,
        "created_at": a.created_at.isoformat(),
    } for a in rows]


@router.post("/api/attachment/delete")
def delete_attachment(payload: dict, user: User = Depends(current_user),
                      s: Session = Depends(session_dep)):
    att = s.get(Attachment, int(payload["attachment_id"]))
    if att is None:
        raise HTTPException(404, "附件不存在")
    conv = s.get(Conversation, att.conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(403, "无权操作该附件")
    p = Path(att.file_path)
    if p.exists():
        p.unlink()
    s.delete(att)
    s.commit()
    return {"deleted": True}


@router.get("/api/attachment/get")
def get_attachment(attachment_id: int, user: User = Depends(current_user),
                   s: Session = Depends(session_dep)):
    att = s.get(Attachment, attachment_id)
    if att is None:
        raise HTTPException(404, "附件不存在")
    conv = s.get(Conversation, att.conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(403, "无权访问该附件")
    p = Path(att.file_path)
    if not p.exists():
        raise HTTPException(404, "文件已丢失")
    return FileResponse(p, filename=att.file_name)


# --------------------------------------------------------------------------
# WebSocket（FR-4.1 ~ FR-4.6）
# --------------------------------------------------------------------------
@router.post("/api/chat/ws-token")
def ws_token(payload: dict, user: User = Depends(current_user),
             s: Session = Depends(session_dep)):
    conv = _own_conv(s, user, int(payload["conversation_id"]))
    tok = secrets.token_urlsafe(32)
    s.add(WebsocketToken(
        user_id=user.id, conversation_id=conv.id, token=tok,
        expires_at=_now() + timedelta(minutes=5),
    ))
    s.commit()
    return {"websocket_token": tok, "expires_in": 300}


def _consume_ws_token(s: Session, token: str) -> WebsocketToken | None:
    """FR-4.3 / NFR-7：一次性消费，用受影响行数判定，避免先查后写竞态。"""
    res = s.execute(
        text("UPDATE websocket_tokens SET consumed_at=:now "
             "WHERE token=:t AND consumed_at IS NULL AND expires_at > :now"),
        {"now": _now(), "t": token},
    )
    if res.rowcount != 1:
        s.rollback()
        from .db import bump_rejection
        bump_rejection(s, "ws_token.reused_or_expired")
        s.commit()
        return None
    s.commit()
    return s.query(WebsocketToken).filter_by(token=token).one_or_none()


@router.websocket("/api/chat/ws/chat")
async def ws_chat(websocket: WebSocket, websocket_token: str = "",
                  conversation_id: int = 0):
    from .db import get_session as new_session

    s = new_session()
    try:
        row = _consume_ws_token(s, websocket_token)
        if row is None:
            await websocket.close(code=4401, reason="token 无效、已用或过期")
            return
        conv = s.get(Conversation, conversation_id)
        if conv is None or conv.user_id != row.user_id:
            await websocket.close(code=4403, reason="无权订阅该会话")
            return
    finally:
        s.close()

    await websocket.accept()
    loop = asyncio.get_running_loop()
    hub.bind_loop(loop)
    q = hub.subscribe(conversation_id)

    # FR-4.5：连接即补发当前任务状态，断连不丢
    s2 = new_session()
    try:
        task = (s2.query(AnalysisTask)
                .filter(AnalysisTask.conversation_id == conversation_id)
                .order_by(AnalysisTask.id.desc()).first())
        if task is not None:
            await websocket.send_json({
                "type": "task_status", "task_id": task.id,
                "task_status": task.task_status,
                "current_step": task.current_step, "replayed": True,
            })
    finally:
        s2.close()

    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20)
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(conversation_id, q)


# --------------------------------------------------------------------------
# 管理（FR-8.1 ~ FR-8.3）
# --------------------------------------------------------------------------
@router.get("/api/admin/config")
def get_config(user: User = Depends(require_admin)):
    return config.all_values()


@router.post("/api/admin/reload")
def reload_config(payload: dict | None = None,
                  user: User = Depends(require_admin)):
    updates = (payload or {}).get("updates") or {}
    if updates:
        config.write_config(updates)
    result = config.reload_config(actor=user.username)
    return {"status": "ok",
            "message": f"配置已重载，变更 {len(result['changed'])} 项",
            "changed": result["changed"]}


@router.get("/api/admin/logs")
def get_logs(task_id: int | None = None, conversation_id: int | None = None,
             level: str | None = None, limit: int = 200,
             user: User = Depends(require_admin),
             s: Session = Depends(session_dep)):
    from .db import TaskLog

    qy = s.query(TaskLog)
    if task_id is not None:
        qy = qy.filter(TaskLog.task_id == task_id)
    if conversation_id is not None:
        qy = qy.filter(TaskLog.conversation_id == conversation_id)
    if level:
        qy = qy.filter(TaskLog.log_level == level)
    rows = qy.order_by(TaskLog.id.desc()).limit(min(limit, 500)).all()
    return [{"id": r.id, "task_id": r.task_id, "log_level": r.log_level,
             "log_type": r.log_type, "log_content": r.log_content,
             "created_at": r.created_at.isoformat()} for r in rows]


@router.get("/api/admin/data-sources")
def list_data_sources(user: User = Depends(require_admin),
                      s: Session = Depends(session_dep)):
    """2.1.2：管理员管理数据源连接。"""
    rows = s.query(DataSource).order_by(DataSource.id).all()
    return [{
        "id": d.id, "name": d.name, "kind": d.kind, "path": d.path,
        "read_only": d.read_only, "status": d.status,
        "last_error": d.last_error, "is_active": d.is_active,
        "last_checked_at": (d.last_checked_at.isoformat()
                            if d.last_checked_at else None),
        "created_at": d.created_at.isoformat(),
    } for d in rows]


@router.post("/api/admin/data-sources")
def create_data_source(payload: dict, user: User = Depends(require_admin),
                       s: Session = Depends(session_dep)):
    """登记数据源。先测连通性，失败也登记但标 error（便于排查，不静默丢弃）。"""
    name = str(payload.get("name", "")).strip()
    path = str(payload.get("path", "")).strip()
    if not name or not path:
        raise HTTPException(400, "name 与 path 都必须提供")
    if s.query(DataSource).filter_by(name=name).one_or_none():
        raise HTTPException(409, f"数据源名称已存在：{name}")

    read_only = bool(payload.get("read_only", True))
    ok, msg = ds.test_connection(path, read_only)

    row = DataSource(
        name=name, kind="sqlite", path=path, read_only=read_only,
        status="ok" if ok else "error",
        last_error="" if ok else msg,
        last_checked_at=_now(),
        is_active=False,
    )
    s.add(row)
    s.commit()

    # 第一个数据源自动激活，避免"加了但没生效"
    if s.query(DataSource).count() == 1:
        row.is_active = True
        s.commit()

    return {"id": row.id, "name": row.name, "status": row.status,
            "message": msg, "is_active": row.is_active}


@router.post("/api/admin/data-sources/{ds_id}/test")
def test_data_source(ds_id: int, user: User = Depends(require_admin),
                     s: Session = Depends(session_dep)):
    row = s.get(DataSource, ds_id)
    if row is None:
        raise HTTPException(404, "数据源不存在")
    ok, msg = ds.test_connection(row.path, row.read_only)
    row.status = "ok" if ok else "error"
    row.last_error = "" if ok else msg
    row.last_checked_at = _now()
    row.updated_at = _now()
    s.commit()
    return {"id": row.id, "status": row.status, "message": msg}


@router.get("/api/admin/data-sources/{ds_id}/inspect")
def inspect_data_source(ds_id: int, user: User = Depends(require_admin),
                        s: Session = Depends(session_dep)):
    """探查表结构，明确报出缺哪些表/字段，以及能否支撑分析。"""
    row = s.get(DataSource, ds_id)
    if row is None:
        raise HTTPException(404, "数据源不存在")
    info = ds.inspect_sqlite(row.path)

    # 缺什么 → 说明哪些分析能力会受影响（impact 已在 inspect 内构建）
    return info


@router.post("/api/admin/data-sources/{ds_id}/activate")
def activate_data_source(ds_id: int, payload: dict | None = None,
                         user: User = Depends(require_admin),
                         s: Session = Depends(session_dep)):
    """切换当前分析使用的数据源。切换前必须能连通。

    数据源不完整时默认拒绝切换（避免切完发现什么都分析不出来），
    传 force=true 可强制切换，但会明确告知后果。
    """
    row = s.get(DataSource, ds_id)
    if row is None:
        raise HTTPException(404, "数据源不存在")

    ok, msg = ds.test_connection(row.path, row.read_only)
    if not ok:
        row.status = "error"
        row.last_error = msg
        s.commit()
        raise HTTPException(400, f"该数据源不可用，未切换：{msg}")

    info = ds.inspect_sqlite(row.path)
    force = bool((payload or {}).get("force"))
    if not info["ready"] and not force:
        raise HTTPException(
            400,
            "该数据源缺少分析必需的表，切换后大部分分析会判「证据不足」："
            + "；".join(info["impact"][:4])
            + "。如确要切换，请传 force=true")

    s.query(DataSource).filter(DataSource.id != ds_id).update(
        {DataSource.is_active: False}, synchronize_session=False)
    row.is_active = True
    row.status = "ok"
    row.last_error = ""
    row.last_checked_at = _now()
    row.updated_at = _now()
    s.commit()

    # 立即重建业务库引擎，切换即时生效
    from .db import biz_engine

    biz_engine()
    return {"id": row.id, "is_active": True,
            "message": f"已切换到 {row.name}",
            "coverage": info["coverage"],
            "warnings": info["impact"][:6]}


@router.delete("/api/admin/data-sources/{ds_id}")
def delete_data_source(ds_id: int, user: User = Depends(require_admin),
                       s: Session = Depends(session_dep)):
    """删除数据源登记。

    允许删除激活中的数据源：删除后自动回落到内置示例库。
    否则只有一个数据源时会死锁——必须先切到别处才能删，
    而"别处"不存在。
    """
    row = s.get(DataSource, ds_id)
    if row is None:
        raise HTTPException(404, "数据源不存在")

    was_active = row.is_active
    s.delete(row)
    s.flush()

    # 若删的是激活源，把剩余的第一个设为激活（没有则回落内置库）
    fallback = None
    if was_active:
        fallback = (s.query(DataSource)
                    .order_by(DataSource.id).first())
        if fallback is not None:
            fallback.is_active = True
    s.commit()

    # 重建引擎，让切换立即生效
    from .db import biz_engine

    biz_engine()

    return {
        "deleted": ds_id,
        "was_active": was_active,
        "fallback": fallback.name if fallback else "内置示例库",
        "message": (f"已删除。当前使用：{fallback.name if fallback else '内置示例库'}"
                    if was_active else "已删除"),
    }


@router.get("/api/admin/users")
def list_users(user: User = Depends(require_admin),
               s: Session = Depends(session_dep)):
    """2.1.2：管理员维护认证配置。"""
    rows = s.query(User).order_by(User.id).all()
    return [{
        "id": u.id, "username": u.username, "display_name": u.display_name,
        "external_user_id": u.external_user_id, "role": u.role,
        "status": u.status,
        "created_at": u.created_at.isoformat(),
    } for u in rows]


@router.post("/api/admin/users")
def create_user(payload: dict, user: User = Depends(require_admin),
                s: Session = Depends(session_dep)):
    from .auth.session import hash_password

    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    role = str(payload.get("role", "analyst"))
    if not username or not password:
        raise HTTPException(400, "用户名和密码都必须提供")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if role not in ("analyst", "admin"):
        raise HTTPException(400, "角色只能是 analyst 或 admin")
    if s.query(User).filter_by(username=username).one_or_none():
        raise HTTPException(409, f"用户名已存在：{username}")

    u = User(
        external_user_id=f"ext-{username}",
        username=username,
        display_name=str(payload.get("display_name") or username),
        password_hash=hash_password(password),
        role=role,
    )
    s.add(u)
    s.commit()
    return {"id": u.id, "username": u.username, "role": u.role}


@router.post("/api/admin/users/{uid}/password")
def reset_password(uid: int, payload: dict, user: User = Depends(require_admin),
                   s: Session = Depends(session_dep)):
    from .auth.session import hash_password

    target = s.get(User, uid)
    if target is None:
        raise HTTPException(404, "用户不存在")
    pw = str(payload.get("password", ""))
    if len(pw) < 6:
        raise HTTPException(400, "密码至少 6 位")
    target.password_hash = hash_password(pw)
    target.updated_at = _now()
    s.commit()
    return {"id": uid, "message": f"{target.username} 的密码已重置"}


@router.post("/api/admin/users/{uid}/role")
def change_role(uid: int, payload: dict, user: User = Depends(require_admin),
                s: Session = Depends(session_dep)):
    target = s.get(User, uid)
    if target is None:
        raise HTTPException(404, "用户不存在")
    role = str(payload.get("role", ""))
    if role not in ("analyst", "admin"):
        raise HTTPException(400, "角色只能是 analyst 或 admin")
    if target.id == user.id and role != "admin":
        raise HTTPException(400, "不能把自己降级，避免锁死管理员入口")
    target.role = role
    target.updated_at = _now()
    s.commit()
    return {"id": uid, "role": role}


@router.post("/api/admin/users/{uid}/status")
def toggle_user_status(uid: int, payload: dict,
                       user: User = Depends(require_admin),
                       s: Session = Depends(session_dep)):
    target = s.get(User, uid)
    if target is None:
        raise HTTPException(404, "用户不存在")
    status = str(payload.get("status", ""))
    if status not in ("active", "disabled"):
        raise HTTPException(400, "状态只能是 active 或 disabled")
    if target.id == user.id and status != "active":
        raise HTTPException(400, "不能停用自己")
    target.status = status
    target.updated_at = _now()
    s.commit()
    return {"id": uid, "status": status}


@router.get("/api/admin/rejections")
def rejections(user: User = Depends(require_admin),
               s: Session = Depends(session_dep)):
    """NFR-2：拒绝路径计数器，为 0 表示该路径未被验证过。"""
    rows = s.query(RejectionCounter).order_by(RejectionCounter.code).all()
    return [{"code": r.code, "count": r.count,
             "last_at": r.last_at.isoformat()} for r in rows]


@router.get("/api/tasks/{task_id}/tools")
def task_tools(task_id: int, user: User = Depends(current_user),
               s: Session = Depends(session_dep)):
    task = s.get(AnalysisTask, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "任务不存在")
    rows = (s.query(ToolInvocation).filter_by(task_id=task_id)
            .order_by(ToolInvocation.id).all())
    return [{
        "id": r.id, "tool_name": r.tool_name, "status": r.status,
        "arguments": json.loads(r.arguments_json or "{}"),
        "result_summary": r.result_summary, "error_message": r.error_message,
        "duration_ms": r.duration_ms, "created_at": r.created_at.isoformat(),
    } for r in rows]


@router.get("/api/attribution/preview")
def parse_preview(q: str = "", user: User = Depends(current_user)):
    """便于调试：展示问题解析结果（不影响生产链路）。"""
    p = parse_question(q)
    return {
        "dimension": p.dimension, "is_causal": p.is_causal,
        "direction": p.direction,
        "period": p.period.as_dict() if p.period else None,
        "missing": p.missing, "clarify_question": p.clarify_question,
    }


@router.get("/api/state/{conversation_id}")
def session_state(conversation_id: int, user: User = Depends(current_user),
                  s: Session = Depends(session_dep)):
    conv = _own_conv(s, user, conversation_id)
    st = s.query(SessionState).filter_by(conversation_id=conv.id).one_or_none()
    if st is None:
        return {"conversation_id": conv.id, "pending_question": ""}
    return {"conversation_id": conv.id, "pending_question": st.pending_question,
            "state": json.loads(st.state_json or "{}")}
