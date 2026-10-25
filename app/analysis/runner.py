"""任务编排：把引擎结果落到 DB、发实时事件、写日志。

关键点：
- 同一会话单 running 任务由 DB 部分唯一索引强制（FR-2.4 / NFR-7）
- 所有工具调用落 tool_invocations，证据才能回溯（FR-6.3）
- 失败必须写日志与 error 事件（NFR-8）
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import config
from ..db import (
    AnalysisResult, AnalysisTask, Attachment, ContextSummary, Conversation,
    Message, SessionState, TaskLog, ToolInvocation, biz_engine,
)
from ..tools import registry as tools
from .engine import AnalysisEngine, AnalysisOutcome


def _now() -> datetime:
    return datetime.now(timezone.utc)


def next_seq(s: Session, conversation_id: int) -> int:
    row = s.execute(
        text("SELECT COALESCE(MAX(seq_no), 0) FROM messages WHERE conversation_id=:c"),
        {"c": conversation_id},
    ).first()
    return int(row[0]) + 1 if row else 1


def add_message(s: Session, conversation_id: int, role: str, content: str,
                message_type: str = "text", tool_name: str | None = None,
                tool_status: str | None = None) -> Message:
    msg = Message(
        conversation_id=conversation_id, role=role, content=content,
        message_type=message_type, tool_name=tool_name, tool_status=tool_status,
        seq_no=next_seq(s, conversation_id),
    )
    s.add(msg)
    conv = s.get(Conversation, conversation_id)
    if conv:
        conv.last_message_at = _now()
        conv.updated_at = _now()
    s.flush()
    return msg


def log(s: Session, task_id: int | None, conversation_id: int | None,
        level: str, log_type: str, content: str) -> None:
    s.add(TaskLog(task_id=task_id, conversation_id=conversation_id,
                  log_level=level, log_type=log_type, log_content=content[:4000]))


def record_tool(s: Session, task_id: int, outcome: tools.ToolOutcome,
                arguments: dict[str, Any]) -> int:
    inv = ToolInvocation(
        task_id=task_id,
        tool_name=outcome.tool,
        arguments_json=json.dumps(arguments, ensure_ascii=False, default=str)[:4000],
        status=outcome.status,
        result_summary=outcome.summary[:2000],
        result_json=json.dumps(outcome.data, ensure_ascii=False, default=str)[:20000],
        error_message=outcome.error[:2000],
        duration_ms=outcome.duration_ms,
    )
    s.add(inv)
    s.flush()
    return inv.id


def markdown_result(outcome_dict: dict[str, Any], question: str) -> str:
    lines = ["# 经营归因分析报告", "", f"**原始问题**：{question}", ""]
    lines += ["## 问题定义", outcome_dict["problem_definition"], ""]

    lines += ["## 关键指标", "", "| 指标 | 值 | 单位 | 周期 | 基线 | 变动 | 口径 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for m in outcome_dict["key_metrics"]:
        lines.append(
            f"| {m.get('metric_name')} | {m.get('metric_value')} | {m.get('metric_unit')} "
            f"| {m.get('metric_period')} | {m.get('baseline_value', '—')} "
            f"| {('%+.2f%%' % m['delta_pct']) if m.get('delta_pct') is not None else '—'} "
            f"| {m.get('definition', '')} |"
        )
    lines.append("")

    if outcome_dict.get("hypotheses"):
        lines += ["## 假设验证", "", "| 假设 | 判定条件 | 结论 | 依据 |", "| --- | --- | --- | --- |"]
        for h in outcome_dict["hypotheses"]:
            lines.append(
                f"| {h['statement']} | {h['criterion']} | {h['verdict']} | {h['reason']} |"
            )
        lines.append("")

    lines += ["## 证据列表", ""]
    for i, e in enumerate(outcome_dict["evidence_list"], 1):
        lines.append(f"{i}. **{e.get('source_name')}**（置信度 {e.get('confidence')}）")
        lines.append(f"   - {e.get('evidence_text')}")
    lines.append("")

    lines += ["## 归因结论", outcome_dict["conclusion_text"], ""]
    lines += ["## 待补充数据", outcome_dict["missing_data_text"] or "- 无", ""]
    lines += ["## 下一步建议", outcome_dict["next_action_text"] or "- 无", ""]
    if outcome_dict.get("degraded_note"):
        lines += ["## 说明", outcome_dict["degraded_note"], ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 任务执行
# --------------------------------------------------------------------------
def run_task(s: Session, task: AnalysisTask, emit) -> None:
    conv = s.get(Conversation, task.conversation_id)
    if conv is None:
        raise RuntimeError("会话不存在")

    engine = biz_engine()
    ev = lambda kind, payload: emit(kind, payload)  # noqa: E731

    try:
        task.task_status = "running"
        task.started_at = _now()
        task.current_step = "启动"
        s.commit()

        ev("message_start", {"task_id": task.id, "conversation_id": task.conversation_id})
        log(s, task.id, task.conversation_id, "info", "step", "任务开始")

        # 2.1.10 要求 8 类实时消息，含 message_delta（增量文本）。
        # 分析型任务没有逐 token 生成，但结论是分段落定的：
        # 在关键节点推送增量文本，使 message_delta 有真实语义而不是空占位。
        def emit_delta(text: str) -> None:
            if text:
                ev("message_delta", {"task_id": task.id, "delta_text": text})

        attachments = [
            {"file_name": a.file_name, "text_excerpt": a.text_excerpt,
             "parse_status": a.parse_status, "file_path": a.file_path,
             "workspace_path": a.file_name}
            for a in s.query(Attachment)
            .filter(Attachment.conversation_id == task.conversation_id).all()
        ]

        state = s.query(SessionState).filter_by(
            conversation_id=task.conversation_id).one_or_none()

        # 上下文摘要读回（2.1.6）：此前只生成不使用，这里真正接入分析上下文
        prior = (s.query(ContextSummary)
                 .filter_by(conversation_id=task.conversation_id)
                 .order_by(ContextSummary.id.desc()).first())
        context_hint = ""
        if prior is not None:
            context_hint = prior.summary_text[:1500]

        engine_inst = AnalysisEngine(engine, ev,
                                     user_id=task.user_id,
                                     conversation_id=task.conversation_id)

        # FR-6.3：每次工具调用落库，证据才能回溯到具体调用
        def _record(payload: dict[str, Any]) -> None:
            try:
                record_tool(s, task.id, payload["tool"], payload["args"])
                s.commit()
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                log(s, task.id, task.conversation_id, "warning", "tool",
                    f"工具调用记录失败: {type(exc).__name__}: {exc}")
                s.commit()

        engine_inst.recorder = _record
        outcome: AnalysisOutcome = engine_inst.run(task.input_text, attachments)

        # 摘要作为前序上下文注入结果说明，便于多轮承接可核查
        if context_hint:
            outcome.context_used = context_hint[:400]

        # 澄清：保存断点（FR-5.7）
        if outcome.needs_clarification:
            merged = {}
            if state and state.state_json:
                try:
                    merged = json.loads(state.state_json)
                except json.JSONDecodeError:
                    merged = {}
            merged.setdefault("clarify_history", []).append(task.input_text)
            if state is None:
                state = SessionState(conversation_id=task.conversation_id)
                s.add(state)
            state.state_json = json.dumps(merged, ensure_ascii=False)
            state.pending_question = outcome.clarify_question
            state.updated_at = _now()

        result_dict = outcome.as_result()
        md = markdown_result(result_dict, task.input_text)

        # 分段推送增量文本（message_delta 有真实内容）
        emit_delta(f"【问题定义】{result_dict['problem_definition']}\n")
        for h in result_dict.get("hypotheses", []):
            emit_delta(f"假设「{h['statement']}」→ {h['verdict']}：{h['reason']}\n")
        emit_delta(f"【结论】{result_dict['conclusion_text']}\n")

        # 结果文件（FR-10.1）
        exports = config.STORAGE_DIR / "exports" / str(task.user_id) / str(task.conversation_id)
        exports.mkdir(parents=True, exist_ok=True)
        fname = f"analysis_{task.id}.md"
        (exports / fname).write_text(md, encoding="utf-8")

        res = s.query(AnalysisResult).filter_by(task_id=task.id).one_or_none()
        if res is None:
            res = AnalysisResult(task_id=task.id, conversation_id=task.conversation_id)
            s.add(res)
        res.problem_definition = result_dict["problem_definition"]
        res.key_metrics_json = json.dumps(result_dict["key_metrics"], ensure_ascii=False)
        res.evidence_list_json = json.dumps(result_dict["evidence_list"], ensure_ascii=False)
        res.conclusion_text = result_dict["conclusion_text"]
        res.missing_data_text = result_dict["missing_data_text"]
        res.next_action_text = result_dict["next_action_text"]
        res.hypotheses_json = json.dumps(result_dict["hypotheses"], ensure_ascii=False)
        res.tool_plan_json = json.dumps(result_dict.get("tool_plan") or {}, ensure_ascii=False)
        res.artifacts_json = json.dumps(result_dict.get("artifacts") or [], ensure_ascii=False)
        res.degraded_note = result_dict.get("degraded_note") or ""
        res.result_markdown = md
        res.result_file_path = str(exports / fname)

        task.task_status = "success"
        task.current_step = "完成"
        task.step_count = outcome.step_count
        task.finished_at = _now()

        add_message(s, task.conversation_id, "assistant",
                    outcome.clarify_question if outcome.needs_clarification
                    else result_dict["conclusion_text"],
                    message_type="clarify" if outcome.needs_clarification else "result")
        s.flush()

        log(s, task.id, task.conversation_id, "info", "step",
            f"任务完成，步骤 {outcome.step_count}，"
            f"假设 {len(result_dict['hypotheses'])} 个，"
            f"证据 {len(result_dict['evidence_list'])} 条")
        ev("result_ready", {"task_id": task.id, "result_id": res.id})
        ev("done", {"task_id": task.id,
                    "finished_at": task.finished_at.isoformat()})

    except Exception as exc:  # noqa: BLE001
        task.task_status = "failed"
        task.error_message = str(exc)[:1000]
        task.finished_at = _now()
        log(s, task.id, task.conversation_id, "error", "task",
            f"分析失败于步骤「{task.current_step}」：{type(exc).__name__}: {exc}")
        ev("error", {"task_id": task.id, "error_message": task.error_message,
                     "failed_step": task.current_step})
    finally:
        s.commit()


def maybe_summarize(s: Session, conversation_id: int) -> ContextSummary | None:
    """FR-7.1~7.4：达到阈值生成摘要，保留可追溯区间。"""
    trigger = int(config.get("summary_trigger_messages"))
    keep = int(config.get("summary_keep_recent"))
    msgs = (s.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.seq_no).all())
    if len(msgs) < trigger:
        return None

    covered = s.query(ContextSummary).filter_by(conversation_id=conversation_id).all()
    covered_upto = max((c.end_seq_no for c in covered), default=0)
    fresh = [m for m in msgs if m.seq_no > covered_upto][:-keep]
    if len(fresh) < 5:
        return None

    # 摘要必须保留口径、已排除假设、数据缺口、用户约束（FR-7.2）
    results = (s.query(AnalysisResult)
               .filter(AnalysisResult.conversation_id == conversation_id)
               .order_by(AnalysisResult.id.desc()).limit(3).all())
    kept: list[str] = []
    for r in results:
        try:
            hs = json.loads(r.hypotheses_json or "[]")
        except json.JSONDecodeError:
            hs = []
        for h in hs:
            kept.append(f"{h.get('statement')} → {h.get('verdict')}（{h.get('reason')}）")
        if r.missing_data_text:
            kept.append("缺口：" + r.missing_data_text.replace("\n", " ")[:200])
    user_turns = [m.content[:80] for m in fresh if m.role == "user"]

    summary = ContextSummary(
        conversation_id=conversation_id,
        start_seq_no=fresh[0].seq_no,
        end_seq_no=fresh[-1].seq_no,
        summary_text="\n".join([
            f"用户提出过的分析诉求：{'；'.join(user_turns[-6:])}",
            f"已核对过的假设与结论：{'；'.join(kept[-8:]) if kept else '暂无'}",
        ])[:4000],
    )
    s.add(summary)
    s.flush()
    return summary


def delete_conversation_cascade(s: Session, conv: Conversation) -> None:
    """FR-2.5 / FR-10.3：级联删除记录与三个目录。

    注意 websocket_tokens 也引用 conversation_id，
    漏掉它会在开启外键约束时抛 FK 错误——这是删除链路必须覆盖的一环。
    """
    import shutil

    from ..db import WebsocketToken

    cid = conv.id
    uid = conv.user_id
    task_ids = [t.id for t in s.query(AnalysisTask).filter_by(conversation_id=cid).all()]
    if task_ids:
        s.query(ToolInvocation).filter(ToolInvocation.task_id.in_(task_ids)).delete(
            synchronize_session=False)
        s.query(TaskLog).filter(TaskLog.task_id.in_(task_ids)).delete(
            synchronize_session=False)
    # 未绑定 task 的日志（如附件解析失败）也要清
    s.query(TaskLog).filter(TaskLog.conversation_id == cid).delete(
        synchronize_session=False)
    s.query(AnalysisResult).filter_by(conversation_id=cid).delete(synchronize_session=False)
    s.query(ContextSummary).filter_by(conversation_id=cid).delete(synchronize_session=False)
    s.query(WebsocketToken).filter_by(conversation_id=cid).delete(synchronize_session=False)
    s.query(Attachment).filter_by(conversation_id=cid).delete(synchronize_session=False)
    s.query(Message).filter_by(conversation_id=cid).delete(synchronize_session=False)
    s.query(AnalysisTask).filter_by(conversation_id=cid).delete(synchronize_session=False)
    s.query(SessionState).filter_by(conversation_id=cid).delete(synchronize_session=False)
    s.delete(conv)
    s.flush()

    for kind in ("uploads", "exports", "workspace"):
        d = config.STORAGE_DIR / kind / str(uid) / str(cid)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
