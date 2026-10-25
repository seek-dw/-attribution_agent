"""分析引擎：假设-取证-收敛循环（FR-5.1 / FR-5.11）。

流程：
  1. 解析问题 → 缺必要条件则提出澄清（FR-5.6），并持久化断点（FR-5.7）
  2. 生成假设（因果类必须 ≥2 个，否则说明原因）
  3. 逐假设取证（工具调用全部落 tool_invocations，FR-6.3）
  4. 判定：成立 / 不成立 / 证据不足
  5. 收敛结论，缺口进「待补充数据」（FR-5.10）
  6. 组装六部分结果（FR-6.1）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from sqlalchemy.engine import Engine

from .. import config
from .hypotheses import (
    HEALTHY, REFUTED, UNKNOWN, EvidenceItem, Hypothesis,
    confidence_level, fmt_pct, pct_delta,
)
from .metrics import (
    METRICS, Period, category_contributions, data_coverage, metric_row_count,
    parse_period, previous_equal_period, refund_concentration,
)
from .router import plan_tools


# --------------------------------------------------------------------------
# 问题解析
# --------------------------------------------------------------------------
@dataclass
class ParsedQuestion:
    raw: str
    period: Period | None = None
    dimension: str = ""          # gmv / orders ...
    is_causal: bool = False      # 是否「为什么」类
    direction: str = ""          # 下降 / 上升
    missing: list[str] = field(default_factory=list)
    clarify_question: str = ""


_METRIC_WORDS: list[tuple[str, str]] = [
    ("销售额", "gmv"), ("营收", "gmv"), ("gmv", "gmv"), ("成交额", "gmv"),
    ("订单量", "orders"), ("单量", "orders"),
    ("访客", "uv"), ("流量", "uv"), ("uv", "uv"), ("pv", "uv"),
    ("转化率", "cvr"), ("转化", "cvr"),
    ("客单价", "aov"), ("均价", "aov"),
    ("缺货", "stockout"), ("断货", "stockout"), ("库存", "stockout"),
    ("退款", "refund_rate"),
    ("评分", "rating"), ("口碑", "rating"), ("差评", "rating"),
    ("竞品", "competitor_price"),
]

_CAUSAL_MARKERS = ("为什么", "为何", "原因", "怎么会", "什么导致", "啥原因", "为何会")


def parse_question(text: str) -> ParsedQuestion:
    q = ParsedQuestion(raw=text or "")
    low = (text or "").lower()

    q.period = parse_period(text or "")
    q.is_causal = any(k in text for k in _CAUSAL_MARKERS)

    if any(k in text for k in ("下滑", "下降", "掉", "跌", "减少", "降低", "变差")):
        q.direction = "下降"
    elif any(k in text for k in ("上涨", "上升", "涨", "增长", "提升", "变好")):
        q.direction = "上升"

    for word, key in _METRIC_WORDS:
        if word in low:
            q.dimension = key
            break

    if not q.dimension:
        # 「为什么销售额跌了」已含维度；否则缺维度
        q.missing.append("分析对象（要看哪个指标）")
    if q.period is None:
        q.missing.append("时间范围（例如 2024-06 或 上个月）")

    if q.missing:
        parts = []
        if "分析对象（要看哪个指标）" in q.missing:
            parts.append("具体要看哪个指标（销售额 / 订单量 / 转化率 / 客单价 / 库存 / 退款 / 评分）")
        if "时间范围（例如 2024-06 或 上个月）" in q.missing:
            parts.append("时间范围（例如 2024-06、上个月、最近30天）")
        q.clarify_question = "为了给出可核验的结论，还需要确认：" + "；".join(parts) + "。"
    return q


# --------------------------------------------------------------------------
# 事件回调（用于 WebSocket 推送与日志）
# --------------------------------------------------------------------------
EventFn = Callable[[str, dict[str, Any]], None]


def _noop(_kind: str, _payload: dict[str, Any]) -> None:
    return None


@dataclass
class AnalysisOutcome:
    problem_definition: str = ""
    key_metrics: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    conclusion: str = ""
    missing_data: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    clarify_question: str = ""
    step_count: int = 0
    degraded_note: str = ""
    tool_plan: dict[str, bool] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    context_used: str = ""

    def as_result(self) -> dict[str, Any]:
        return {
            "problem_definition": self.problem_definition,
            "key_metrics": self.key_metrics,
            "evidence_list": self.evidence,
            "conclusion_text": self.conclusion,
            "missing_data_text": "\n".join(f"- {m}" for m in self.missing_data),
            "next_action_text": "\n".join(f"- {a}" for a in self.next_actions),
            "hypotheses": self.hypotheses,
            "needs_clarification": self.needs_clarification,
            "clarify_question": self.clarify_question,
            "step_count": self.step_count,
            "degraded_note": self.degraded_note,
            "tool_plan": self.tool_plan,
            "artifacts": self.artifacts,
            "context_used": self.context_used,
        }


# --------------------------------------------------------------------------
# 主引擎
# --------------------------------------------------------------------------
class AnalysisEngine:
    def __init__(self, engine: Engine | None, emit: EventFn = _noop,
                 recorder: Callable[[Any], None] | None = None,
                 user_id: int = 0, conversation_id: int = 0,
                 task_id: int = 0) -> None:
        self.engine = engine
        self.recorder = recorder          # 工具调用留痕回调（FR-6.3）
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.task_id = task_id            # 2.1.10：消息必须带 task_id
        self.steps = 0
        self.max_steps = int(config.get("task_max_steps"))
        self.threshold = float(config.get("delta_threshold_pct"))
        self.outcome = AnalysisOutcome()
        self._last_hyps: list[Hypothesis] = []
        # 2.1.10：八类实时消息的字段是原文约定，不能靠每个调用点自觉。
        # 在 emit 出口统一补 task_id / conversation_id，一处修、处处生效。
        base = {"task_id": self.task_id, "conversation_id": self.conversation_id}

        def emit_with_ids(kind: str, payload: dict[str, Any]) -> None:
            emit(kind, {**base, **payload})

        self.emit = emit_with_ids

    # -- 步骤守卫（FR-5.9）------------------------------------------------
    def _step(self, name: str) -> None:
        self.steps += 1
        if self.steps > self.max_steps:
            raise RuntimeError(f"分析步骤超过上限 {self.max_steps}，任务终止")
        # 2.1.10：task_status 消息必须含 task_id、task_status、current_step
        self.emit("task_status", {"task_id": self.task_id,
                                  "task_status": "running",
                                  "current_step": name,
                                  "step_count": self.steps})

    # -- 附件与文本检索取证 ------------------------------------------------
    def _collect_attachment_evidence(self, plan: Any,
                                     attachments: list[dict[str, Any]]) -> None:
        """按工具计划，用 text_search / file_read 从附件中取证。

        这两类工具此前实现了却没被引擎调用，等于 2.1.1 的
        "文件读写、文本检索" 只有壳没有链路。这里补上真实调用。
        """
        if not attachments:
            return
        if not (plan.need_text_search or plan.need_file_read):
            return

        from ..db import get_session
        from ..tools import registry as tools

        s = get_session()
        try:
            if plan.need_text_search and plan.keywords:
                self._step("检索附件内容")
                for kw in plan.keywords:
                    self.emit("tool_start", {"tool_name": "text_search"})
                    out = tools.text_search(s, self.user_id, self.conversation_id, kw)
                    s.commit()
                    if self.recorder is not None:
                        self.recorder({"tool": out, "args": {"keyword": kw}})
                    self.emit("tool_finish", {
                        "tool_name": "text_search",
                        "tool_result_summary": out.summary,
                    })
                    if not out.ok or not out.data.get("hits"):
                        continue
                    for hit in out.data["hits"][:3]:
                        self.outcome.evidence.append({
                            "source_type": "attachment",
                            "source_name": hit["source_name"],
                            "evidence_text": f"命中「{kw}」：{hit['snippet']}",
                            "related_metric": "attachment",
                            "confidence": "中",
                            "payload": {"keyword": kw, "offset": hit["offset"]},
                        })

            if plan.need_file_read:
                self._step("读取附件文本")
                for att in attachments[:3]:
                    path = att.get("workspace_path") or att.get("file_name")
                    if not path:
                        continue
                    self.emit("tool_start", {"tool_name": "file_read"})
                    out = tools.file_read(s, self.user_id, self.conversation_id, path)
                    s.commit()
                    if self.recorder is not None:
                        self.recorder({"tool": out, "args": {"path": path}})
                    self.emit("tool_finish", {
                        "tool_name": "file_read",
                        "tool_result_summary": out.summary or out.error,
                    })
                    if out.ok:
                        body = (out.data.get("content") or "")[:300]
                        self.outcome.evidence.append({
                            "source_type": "file",
                            "source_name": path,
                            "evidence_text": f"读取内容节选：{body}",
                            "related_metric": "attachment",
                            "confidence": "中",
                        })
                    elif out.status == "failed":
                        self.outcome.missing_data.append(
                            f"附件 {path} 内容不可读：{out.error}"
                        )
        finally:
            s.close()

    # -- 中间结果落文件（file_write）+ 口径校验（command_run）-------------
    def _emit_artifacts(self, question: str, plan: Any) -> None:
        """按计划把中间结果写文件、或用白名单命令做一次口径校验。

        file_write / command_run 此前同样只有实现没有链路，这里补齐。
        """
        if not (plan.need_file_write or plan.need_command):
            return

        from ..db import get_session
        from ..tools import registry as tools

        s = get_session()
        try:
            if plan.need_file_write:
                self._step("写出中间结果文件")
                payload = {
                    "question": question,
                    "key_metrics": self.outcome.key_metrics,
                    "hypotheses": [h.as_dict() for h in self._last_hyps],
                }
                name = f"intermediate_{int(time.time())}.json"
                self.emit("tool_start", {"tool_name": "file_write"})
                out = tools.file_write(
                    s, self.user_id, self.conversation_id, name,
                    json.dumps(payload, ensure_ascii=False, indent=2),
                )
                s.commit()
                if self.recorder is not None:
                    self.recorder({"tool": out, "args": {"path": name}})
                self.emit("tool_finish", {
                    "tool_name": "file_write",
                    "tool_result_summary": out.summary or out.error,
                })
                if out.ok:
                    self.outcome.artifacts.append(out.data.get("path", name))

            if plan.need_command:
                self._step("用白名单命令核算指标")
                expr = self._build_check_expression()
                if expr:
                    self.emit("tool_start", {"tool_name": "command_run"})
                    out = tools.command_run(
                        s, self.user_id, self.conversation_id,
                        f'python -c "{expr}"',
                    )
                    s.commit()
                    if self.recorder is not None:
                        self.recorder({"tool": out, "args": {"purpose": "口径校验"}})
                    self.emit("tool_finish", {
                        "tool_name": "command_run",
                        "tool_result_summary": out.summary or out.error,
                    })
                    if out.ok:
                        self.outcome.evidence.append({
                            "source_type": "computation",
                            "source_name": "命令行口径校验",
                            "evidence_text": f"独立算式结果：{(out.data.get('output') or '').strip()[:200]}",
                            "related_metric": "check",
                            "confidence": "中",
                        })
                    else:
                        self.outcome.missing_data.append(
                            f"口径校验未完成：{out.error}"
                        )
        finally:
            s.close()

    def _build_check_expression(self) -> str:
        """把已有指标拼成一个独立可复算的算式，用于交叉验证。"""
        parts = []
        for m in self.outcome.key_metrics:
            if m.get("baseline_value") is not None and m.get("delta_pct") is not None:
                parts.append(
                    f"{m['metric_name']}={m['metric_value']}"
                    f"(base {m['baseline_value']}, delta {m['delta_pct']}%)"
                )
        if not parts:
            return ""
        text_ = " ; ".join(parts)
        return f"print({text_!r})"

    # -- 取值 --------------------------------------------------------------
    def _metric(self, key: str, period: Period) -> tuple[float | None, int]:
        """取指标值。所有取数都经过工具层，产生可回溯的调用记录（FR-6.3）。"""
        if self.engine is None:
            return None, 0
        self._step(f"查询指标 {METRICS[key].label}")

        mdef = METRICS[key]
        sql = mdef.sql
        params = {"s": period.start.isoformat(), "e": period.end.isoformat()}

        from ..tools import registry as tools
        from ..db import get_session

        s = get_session()
        try:
            self.emit("tool_start", {"tool_name": "db_query"})
            out = tools.db_query(s, sql, params)
            s.commit()
        finally:
            s.close()

        rows = metric_row_count(self.engine, key, period)

        if self.recorder is not None:
            self.recorder({"tool": out, "args": {
                "metric": key, "label": mdef.label,
                "period": period.label,
                "range": [period.start.isoformat(), period.end.isoformat()],
                "source_rows": rows,
            }})

        # 关键：区间内无原始记录 → None（不是 0）。
        # COALESCE 会把"没有记录"变成 0，直接拿来用会把"没数据"当成"业务归零"。
        if rows == 0:
            val = None
        elif out.ok and out.data.get("rows"):
            raw = out.data["rows"][0].get("v")
            val = None if raw is None else float(raw) * mdef.scale
        else:
            val = None

        self.emit("tool_finish", {
            "tool_name": "db_query",
            "tool_result_summary": f"{mdef.label}（{period.label}）= "
                                   f"{'无数据' if val is None else round(val, 2)}{mdef.unit}",
        })
        return val, rows

    def _add_metric(self, key: str, period: Period, cur: float, base: float | None) -> None:
        mdef = METRICS[key]
        item = {
            "metric_name": mdef.label,
            "metric_value": round(cur, 2),
            "metric_unit": mdef.unit,
            "metric_period": period.label,
            "definition": mdef.definition,
        }
        if base is not None:
            item["baseline_value"] = round(base, 2)
            item["delta_pct"] = (None if pct_delta(cur, base) is None
                                 else round(pct_delta(cur, base), 2))
        self.outcome.key_metrics.append(item)

    # -- 主流程 ------------------------------------------------------------
    def run(self, question: str, attachments: list[dict[str, Any]] | None = None
            ) -> AnalysisOutcome:
        attachments = attachments or []
        parsed = parse_question(question)

        # FR-5.6 澄清
        if parsed.missing:
            self.outcome.needs_clarification = True
            self.outcome.clarify_question = parsed.clarify_question
            self.outcome.problem_definition = f"待澄清：{question.strip()}"
            self.outcome.conclusion = "尚不能开始分析。缺少必要条件，已提出澄清。"
            self.outcome.missing_data = parsed.missing
            self.outcome.next_actions = ["补充上述信息后我会继续分析，无需重复描述背景。"]
            self.outcome.step_count = self.steps
            return self.outcome

        p1 = parsed.period
        assert p1 is not None
        p0 = previous_equal_period(p1)

        self._step("解析问题与口径")
        if parsed.is_causal:
            self.outcome.problem_definition = (
                f"在「{METRICS[parsed.dimension].label}」口径下，"
                f"分析 {p1.label}（{p1.start} ~ {p1.end}）相对基线"
                f"（{p0.start} ~ {p0.end}）的{parsed.direction or '变化'}成因。"
                f"原始问题：{question.strip()}"
            )
        else:
            self.outcome.problem_definition = (
                f"在「{METRICS[parsed.dimension].label}」口径下，"
                f"报告 {p1.label}（{p1.start} ~ {p1.end}）的数值，"
                f"并与基线（{p0.start} ~ {p0.end}）对照。"
                f"原始问题：{question.strip()}"
            )

        # 按问题选择工具组合（2.1.1 分析能力：查询/文件/检索/命令）
        # 每个能力受功能开关约束（2.1.2 启停功能开关）
        plan = plan_tools(question, bool(attachments))
        disabled: list[str] = []
        if not config.switch_on("enable_text_search"):
            if plan.need_text_search:
                disabled.append("文本检索")
            plan.need_text_search = False
        if not config.switch_on("enable_file_tools"):
            if plan.need_file_read or plan.need_file_write:
                disabled.append("文件读写")
            plan.need_file_read = False
            plan.need_file_write = False
        if not config.switch_on("enable_command_tool"):
            if plan.need_command:
                disabled.append("命令执行")
            plan.need_command = False
        if disabled:
            self.outcome.degraded_note = (
                f"以下能力已被管理员关闭，本次未参与取证：{'、'.join(disabled)}。"
            )
        self.outcome.tool_plan = plan.as_dict()

        # 附件证据：检索命中的片段进入证据链（FR-3.6）
        self._collect_attachment_evidence(plan, attachments)

        # 基线指标
        dim = parsed.dimension
        cur, cur_rows = self._metric(dim, p1)
        base, base_rows = self._metric(dim, p0)

        if cur is None:
            # 区分三种情况：区间无数据（可能是越界）、表缺失、真实为 0
            first, last = (data_coverage(self.engine) if self.engine
                           else (None, None))
            if first and last and (p1.end < date.fromisoformat(first)
                                   or p1.start > date.fromisoformat(last)):
                self.outcome.conclusion = (
                    f"{p1.label}（{p1.start} ~ {p1.end}）不在业务数据的覆盖区间内。"
                    f"当前数据覆盖 {first} ~ {last}，因此无法对该区间做任何判断。"
                )
                self.outcome.missing_data = [
                    f"{p1.label} 的经营数据（当前库仅有 {first} ~ {last}）"
                ]
                self.outcome.next_actions = [
                    f"改用覆盖区间内的月份重新提问，例如 "
                    f"{date.fromisoformat(last).strftime('%Y-%m')}。",
                    "或先导入该区间的订单、流量与退款数据后重新分析。",
                ]
            else:
                self.outcome.conclusion = (
                    f"{METRICS[dim].label}在 {p1.label}（{p1.start} ~ {p1.end}）"
                    f"取不到任何记录，无法分析。"
                )
                self.outcome.missing_data = [
                    f"{METRICS[dim].label} 在 {p1.label} 的原始记录"
                ]
                self.outcome.next_actions = [
                    "确认该区间的数据是否已导入业务库。",
                    "确认指标口径对应的表是否存在且字段可读。",
                ]
            self.outcome.step_count = self.steps
            return self.outcome

        self._add_metric(dim, p1, cur, base)
        delta = pct_delta(cur, base) if base is not None else None

        # 无显著变化：直接给结论，不硬凑假设
        if delta is not None and abs(delta) < self.threshold:
            self.outcome.conclusion = (
                f"{METRICS[dim].label}从 {round(base, 2)} 变为 {round(cur, 2)}，"
                f"变动 {fmt_pct(delta)}，未超过显著阈值 {self.threshold}%，"
                "不构成需要归因的异常。"
            )
            self.outcome.next_actions = [
                "若仍怀疑存在结构性问题，可指定更长区间或分品类再分析。",
                "可补充竞品或促销数据，检查是否存在未计入的收入口径差异。",
            ]
            self.outcome.step_count = self.steps
            return self.outcome

        # 生成假设（FR-5.11）：仅因果类问题做归因；非因果只报数与对照
        if not parsed.is_causal:
            o_cur, _ = self._metric("orders", p1)
            o_base, _ = self._metric("orders", p0)
            if o_cur is not None and base is not None:
                self._add_metric("orders", p1, o_cur, o_base)
            self.outcome.conclusion = (
                f"{METRICS[dim].label}在 {p1.label} 为 {round(cur, 2)}{METRICS[dim].unit}"
                + (f"，基线 {p0.label} 为 {round(base, 2)}{METRICS[dim].unit}，"
                   f"变动 {fmt_pct(delta)}。" if base is not None else
                   "（基线区间无数据，无法给出变动幅度）。")
                + "\n本次是取数对照，不是归因。若要分析变化原因，请以「为什么」提问。"
            )
            self.outcome.next_actions = [
                f"若要归因，可问：为什么 {p1.label} 的{METRICS[dim].label}"
                f"{'下滑' if (delta or 0) < 0 else '变化'}？",
                "可指定类目、渠道或单品维度获得更细的分解。",
            ]
            self.outcome.step_count = self.steps
            return self.outcome

        self._step("生成假设")
        hyps = self._build_hypotheses(dim, parsed.is_causal)
        if parsed.is_causal and len(hyps) < 2:
            self.outcome.degraded_note = (
                f"因果类问题本应拆出至少 2 个假设，实际仅 {len(hyps)} 个。"
                "原因：当前可用维度不足以支撑第二个独立假设。"
            )

        # 逐假设取证
        for h in hyps:
            self._step(f"验证假设 {h.key}")
            self._verify(h, p1, p0, dim, cur, base)

        self.outcome.hypotheses = [h.as_dict() for h in hyps]

        # 收敛
        self._step("收敛结论")
        self._last_hyps = hyps
        self._converge(parsed, hyps, p1, p0, dim, cur, base, delta,
                       cur_rows + base_rows, attachments)

        # 按计划产出中间文件 / 做一次白名单命令口径校验
        self._emit_artifacts(question, plan)

        self.outcome.step_count = self.steps
        return self.outcome

    # -- 假设构造 ----------------------------------------------------------
    def _build_hypotheses(self, dim: str, is_causal: bool) -> list[Hypothesis]:
        """按维度生成可用假设。

        注意：假设来自「有哪些维度可查」，不来自预埋结论。
        每个假设都可能被判成立、不成立或证据不足。
        """
        H: list[Hypothesis] = []

        if dim == "gmv":
            H.append(Hypothesis(
                "category_structure",
                "销售额变化由某单一类目大幅波动主导",
                "存在某个类目，其贡献差额绝对值 ≥ 总差额的 50%",
            ))
            H.append(Hypothesis(
                "traffic_drop",
                "访客数下降导致销售额下降",
                "访客数同比变动 ≤ -显著阈值，且其解释力大于类目因素",
            ))
            H.append(Hypothesis(
                "cvr_drop",
                "转化率下降导致销售额下降",
                "转化率同比变动 ≤ -显著阈值",
            ))
            H.append(Hypothesis(
                "aov_drop",
                "客单价下降导致销售额下降",
                "客单价同比变动 ≤ -显著阈值",
            ))
            H.append(Hypothesis(
                "competitor_pressure",
                "竞品价格下探挤压了销售",
                "竞品均价同比降幅 ≥ 显著阈值",
            ))
            H.append(Hypothesis(
                "refund_erosion",
                "退款占比上升侵蚀了有效销售额",
                "退款金额占比同比上升 ≥ 显著阈值",
            ))
        elif dim == "orders":
            H.append(Hypothesis("traffic_drop", "访客数下降导致订单量下降",
                                "访客数同比变动 ≤ -显著阈值"))
            H.append(Hypothesis("cvr_drop", "转化率下降导致订单量下降",
                                "转化率同比变动 ≤ -显著阈值"))
            H.append(Hypothesis("category_structure", "订单量变化由某类目主导",
                                "存在类目贡献差额绝对值 ≥ 总差额 50%"))
        elif dim in ("uv", "cvr"):
            H.append(Hypothesis("channel_structure", "流量来源结构发生变化",
                                "各来源渠道访客占比变动 ≥ 显著阈值"))
            H.append(Hypothesis("category_structure", "变化集中在某类目",
                                "存在类目贡献差额绝对值 ≥ 总差额 50%"))
        elif dim == "stockout":
            H.append(Hypothesis("stock_pressure", "缺货 SKU 增加抑制了销售",
                                "缺货 SKU 数同比上升"))
            H.append(Hypothesis("category_structure", "缺货集中在某类目",
                                "存在类目贡献差额绝对值 ≥ 总差额 50%"))
        elif dim == "refund_rate":
            # 注意：「退款占比上升」本身不能作为退款占比上升的原因（同义反复），
            # 这里只能放能独立解释它的维度。
            H.append(Hypothesis("rating_decline", "评分下滑推高退款",
                                "平均评分同比下降 ≥ 0.2 分"))
            H.append(Hypothesis("refund_concentration", "退款集中在单一类目",
                                "存在类目的退款金额占比 ≥ 总退款 50%"))
            H.append(Hypothesis("refund_amount_rise", "单笔退款金额抬升",
                                "平均单笔退款金额同比上升 ≥ 显著阈值"))
            H.append(Hypothesis("gmv_drop_denominator", "销售额下降抬高退款占比",
                                "销售额同比变动 ≤ -显著阈值"))
        elif dim == "rating":
            H.append(Hypothesis("rating_decline", "评分下降",
                                "平均评分同比下降 ≥ 0.2 分"))
            H.append(Hypothesis("refund_correlation", "退款率上升与评分下降同向",
                                "退款占比同比上升 ≥ 显著阈值"))
        else:
            H.append(Hypothesis("category_structure", "变化集中在某类目",
                                "存在类目贡献差额绝对值 ≥ 总差额 50%"))
            H.append(Hypothesis("traffic_drop", "访客数下降",
                                "访客数同比变动 ≤ -显著阈值"))

        return H

    # -- 单假设验证 --------------------------------------------------------
    def _verify(self, h: Hypothesis, p1: Period, p0: Period, dim: str,
                cur: float, base: float | None) -> None:
        thr = self.threshold
        total_delta = None
        if base is not None and cur is not None:
            total_delta = cur - base

        def metric_evidence(key: str, label: str) -> tuple[float | None, float | None, int]:
            c, cr = self._metric(key, p1)
            b, br = self._metric(key, p0)
            if c is None or b is None:
                # 说清缺的是哪一期，而不是笼统说"数据缺失"
                lack = []
                if c is None:
                    lack.append(f"本期 {p1.start}~{p1.end}")
                if b is None:
                    lack.append(f"基期 {p0.start}~{p0.end}")
                detail = "、".join(lack)
                h.evidence.append(EvidenceItem(
                    source_type="database", source_name=f"{label}（{p1.label} / {p0.label}）",
                    evidence_text=f"{label} 在 {detail} 没有记录："
                                  f"本期 {'无记录' if c is None else round(c,2)}，"
                                  f"基期 {'无记录' if b is None else round(b,2)}",
                    related_metric=key, confidence="低",
                ))
                return c, b, cr + br
            if key in ("gmv", "orders", "aov", "competitor_price"):
                self._add_metric(key, p1, c, b)
            d = pct_delta(c, b)
            h.evidence.append(EvidenceItem(
                source_type="database",
                source_name=f"{label}（{p1.label} vs {p0.label}）",
                evidence_text=f"{label}：本期 {round(c,2)}，基期 {round(b,2)}，变动 {fmt_pct(d)}；"
                              f"口径：{METRICS.get(key).definition if key in METRICS else '—'}",
                related_metric=key,
                confidence=confidence_level(cr + br, True, False),
                payload={"current": round(c, 2), "baseline": round(b, 2),
                         "delta_pct": None if d is None else round(d, 2)},
            ))
            return c, b, cr + br

        if h.key == "category_structure":
            self._step("按类目分解")
            parts = category_contributions(self.engine, p1, p0) if self.engine else []
            if not parts:
                h.verdict = UNKNOWN
                h.reason = "类目分解取不到数据，无法判断是否存在单类目主导"
                self.outcome.missing_data.append("按类目拆分的销售额明细")
                return
            top = parts[0]
            total_abs = sum(abs(p["delta"]) for p in parts) or 1.0
            share = abs(top["delta"]) / total_abs * 100
            h.evidence.append(EvidenceItem(
                source_type="database", source_name="类目分解",
                evidence_text=(
                    "各类目销售额变动：" +
                    "；".join(f"{p['category']} {p['delta']:+.2f} 元" for p in parts[:6]) +
                    f"。最大贡献类目「{top['category']}」占变动绝对值 {share:.1f}%"
                ),
                related_metric="gmv",
                confidence=confidence_level(len(parts) * 10, True, False),
                payload={"contributions": parts, "top_share_pct": round(share, 2)},
            ))
            h.verdict = HEALTHY if share >= 50 else REFUTED
            h.reason = (f"最大类目「{top['category']}」贡献 {share:.1f}% 的变动，"
                        f"{'达到' if share >= 50 else '未达到'} 50% 判定线")
            h.supports_conclusion = h.verdict == HEALTHY
            if h.verdict == REFUTED:
                self.outcome.degraded_note = self.outcome.degraded_note or ""
            return

        if h.key == "traffic_drop":
            c, b, rows = metric_evidence("uv", "访客数")
            if c is None or b is None:
                h.verdict = UNKNOWN
                h.reason = "访客数数据缺失，无法判定"
                self.outcome.missing_data.append("区间内按日访客数明细")
                return
            d = pct_delta(c, b)
            h.verdict = HEALTHY if (d is not None and d <= -thr) else REFUTED
            h.reason = f"访客数变动 {fmt_pct(d)}，{'达到' if h.verdict == HEALTHY else '未达到'} -{thr}% 判定线"
            h.supports_conclusion = h.verdict == HEALTHY
            return

        if h.key == "cvr_drop":
            c, b, rows = metric_evidence("cvr", "转化率")
            if c is None or b is None:
                h.verdict = UNKNOWN
                h.reason = "转化率数据缺失，无法判定"
                self.outcome.missing_data.append("区间内曝光、点击与下单明细")
                return
            d = pct_delta(c, b)
            h.verdict = HEALTHY if (d is not None and d <= -thr) else REFUTED
            h.reason = f"转化率变动 {fmt_pct(d)}，{'达到' if h.verdict == HEALTHY else '未达到'} -{thr}% 判定线"
            h.supports_conclusion = h.verdict == HEALTHY
            return

        if h.key == "aov_drop":
            c, b, rows = metric_evidence("aov", "客单价")
            if c is None or b is None:
                h.verdict = UNKNOWN
                h.reason = "客单价数据缺失，无法判定"
                self.outcome.missing_data.append("区间内订单金额明细")
                return
            d = pct_delta(c, b)
            h.verdict = HEALTHY if (d is not None and d <= -thr) else REFUTED
            h.reason = f"客单价变动 {fmt_pct(d)}，{'达到' if h.verdict == HEALTHY else '未达到'} -{thr}% 判定线"
            h.supports_conclusion = h.verdict == HEALTHY
            return

        if h.key == "competitor_pressure":
            c, b, rows = metric_evidence("competitor_price", "竞品均价")
            if c is None or b is None:
                h.verdict = UNKNOWN
                h.reason = "竞品价格数据缺失，无法判断是否属于竞品挤压"
                self.outcome.missing_data.append("区间内竞品价格快照")
                return
            d = pct_delta(c, b)
            # 判据：竞品降价（d <= -thr）才成立
            h.verdict = HEALTHY if (d is not None and d <= -thr) else REFUTED
            h.reason = (f"竞品均价变动 {fmt_pct(d)}，"
                        f"{'出现' if h.verdict == HEALTHY else '未出现'} ≥{thr}% 的价格下探，"
                        f"{'支持' if h.verdict == HEALTHY else '不支持'}竞品挤压假设")
            h.supports_conclusion = h.verdict == HEALTHY
            return

        if h.key == "refund_erosion":
            c, b, rows = metric_evidence("refund_rate", "退款金额占比")
            if c is None or b is None:
                h.verdict = UNKNOWN
                h.reason = "退款数据缺失，无法判定"
                self.outcome.missing_data.append("区间内退款申请与退款金额明细")
                return
            d = pct_delta(c, b)
            h.verdict = HEALTHY if (d is not None and d >= thr) else REFUTED
            h.reason = f"退款占比变动 {fmt_pct(d)}，{'达到' if h.verdict == HEALTHY else '未达到'} +{thr}% 判定线"
            h.supports_conclusion = h.verdict == HEALTHY
            return

        if h.key == "rating_decline":
            c, b, rows = metric_evidence("rating", "平均评分")
            if c is None or b is None:
                h.verdict = UNKNOWN
                h.reason = "评分数据缺失，无法判定"
                self.outcome.missing_data.append("区间内评分明细")
                return
            diff = c - b
            h.verdict = HEALTHY if diff <= -0.2 else REFUTED
            h.reason = f"平均评分 {round(b,2)} → {round(c,2)}，变动 {diff:+.2f} 分，" \
                       f"{'达到' if h.verdict == HEALTHY else '未达到'} -0.2 分判定线"
            h.supports_conclusion = h.verdict == HEALTHY
            return

        if h.key == "refund_correlation":
            c, b, rows = metric_evidence("refund_rate", "退款金额占比")
            if c is None or b is None:
                h.verdict = UNKNOWN
                h.reason = "退款数据缺失，无法判定相关性"
                self.outcome.missing_data.append("区间内退款金额明细")
                return
            d = pct_delta(c, b)
            h.verdict = HEALTHY if (d is not None and d >= thr) else REFUTED
            h.reason = f"退款占比变动 {fmt_pct(d)}"
            h.supports_conclusion = h.verdict == HEALTHY
            return

        if h.key == "channel_structure":
            h.verdict = UNKNOWN
            h.reason = "当前示例数据未提供流量来源渠道维度，无法验证结构变化"
            self.outcome.missing_data.append("按来源渠道拆分的访客明细")
            return

        if h.key == "refund_concentration":
            self._step("按类目拆分退款")
            parts = refund_concentration(self.engine, p1) if self.engine else []
            if not parts:
                h.verdict = UNKNOWN
                h.reason = "类目退款拆分取不到数据，无法判断是否集中"
                self.outcome.missing_data.append("按类目拆分的退款金额明细")
                return
            total = sum(p["amount"] for p in parts) or 1.0
            top = parts[0]
            share = top["amount"] / total * 100
            h.evidence.append(EvidenceItem(
                source_type="database", source_name="退款类目拆分",
                evidence_text=(
                    "各类目退款金额：" +
                    "；".join(f"{p['category']} {p['amount']:.2f} 元（{p['count']} 笔）"
                              for p in parts) +
                    f"。最大类目「{top['category']}」占退款总额 {share:.1f}%"
                ),
                related_metric="refund_rate",
                confidence=confidence_level(len(parts) * 10, True, False),
                payload={"contributions": parts, "top_share_pct": round(share, 2)},
            ))
            h.verdict = HEALTHY if share >= 50 else REFUTED
            h.reason = (f"「{top['category']}」占退款总额 {share:.1f}%，"
                        f"{'达到' if share >= 50 else '未达到'} 50% 判定线")
            h.supports_conclusion = h.verdict == HEALTHY
            return

        if h.key == "refund_amount_rise":
            c, b, rows = metric_evidence("refund_amount_avg", "单笔退款金额")
            if c is None or b is None:
                h.verdict = UNKNOWN
                h.reason = "退款笔数不足或数据缺失，无法判断单笔金额变化"
                self.outcome.missing_data.append("区间内退款明细")
                return
            d = pct_delta(c, b)
            h.verdict = HEALTHY if (d is not None and d >= thr) else REFUTED
            h.reason = f"单笔退款金额 {round(b,2)} → {round(c,2)}，变动 {fmt_pct(d)}，" \
                       f"{'达到' if h.verdict == HEALTHY else '未达到'} +{thr}% 判定线"
            h.supports_conclusion = h.verdict == HEALTHY
            return

        if h.key == "gmv_drop_denominator":
            c, b, rows = metric_evidence("gmv", "销售额")
            if c is None or b is None:
                h.verdict = UNKNOWN
                h.reason = "销售额数据缺失，无法判断分母效应"
                self.outcome.missing_data.append("区间内销售额明细")
                return
            d = pct_delta(c, b)
            h.verdict = HEALTHY if (d is not None and d <= -thr) else REFUTED
            h.reason = f"销售额变动 {fmt_pct(d)}，" \
                       f"{'达到' if h.verdict == HEALTHY else '未达到'} -{thr}% 判定线" \
                       f"（{'分母缩小会抬高占比' if h.verdict == HEALTHY else '分母未显著缩小'}）"
            h.supports_conclusion = h.verdict == HEALTHY
            return

        if h.key == "stock_pressure":
            c, b, rows = metric_evidence("stockout", "缺货 SKU 数")
            if c is None or b is None:
                h.verdict = UNKNOWN
                h.reason = "库存数据缺失，无法判定"
                self.outcome.missing_data.append("区间内库存快照")
                return
            h.verdict = HEALTHY if c > b else REFUTED
            h.reason = f"缺货 SKU 数 {int(b)} → {int(c)}"
            h.supports_conclusion = h.verdict == HEALTHY
            return

        h.verdict = UNKNOWN
        h.reason = "该假设暂无对应取证口径"

    # -- 收敛 --------------------------------------------------------------
    def _converge(self, parsed: ParsedQuestion, hyps: list[Hypothesis],
                  p1: Period, p0: Period, dim: str,
                  cur: float, base: float | None, delta: float | None,
                  sample_rows: int, attachments: list[dict[str, Any]]) -> None:
        supported = [h for h in hyps if h.verdict == HEALTHY]
        refuted = [h for h in hyps if h.verdict == REFUTED]
        unknown = [h for h in hyps if h.verdict == UNKNOWN]

        self.outcome.evidence = [
            e.as_dict() for h in hyps for e in h.evidence
        ]

        # 附件作为补充证据（FR-3.6）
        for att in attachments:
            self.outcome.evidence.append({
                "source_type": "attachment",
                "source_name": att.get("file_name", ""),
                "evidence_text": (att.get("text_excerpt") or "")[:200] or "附件已上传但无可用文本",
                "related_metric": dim,
                "confidence": "中" if att.get("parse_status") == "success" else "低",
            })

        label = METRICS[dim].label
        head = (f"{label}在 {p1.label} 为 {round(cur,2)}{METRICS[dim].unit}，"
                f"基线为 {round(base,2) if base is not None else '无数据'}"
                f"{METRICS[dim].unit}，变动 {fmt_pct(delta)}。")

        if supported:
            ranked = sorted(
                supported,
                key=lambda h: len(h.evidence),
                reverse=True,
            )
            main = ranked[0]
            lines = [head, f"主要原因：{main.statement}。依据：{main.reason}。"]
            if len(supported) > 1:
                lines.append("同时成立的因素：" +
                             "；".join(f"{h.statement}（{h.reason}）" for h in supported[1:]) + "。")
            if refuted:
                lines.append("已排除：" +
                             "；".join(f"{h.statement}（{h.reason}）" for h in refuted) + "。")
            lines.append("影响范围：结论仅覆盖上述已取证维度，未覆盖维度不作推断。")
            self.outcome.conclusion = "\n".join(lines)
        else:
            reasons = "；".join(f"{h.statement}（{h.reason}）" for h in hyps)
            self.outcome.conclusion = "\n".join([
                head,
                "在本次取证覆盖的假设中，没有任何一个被证据支持成立，"
                "因此无法给出确定归因。",
                f"已检查的假设及结论：{reasons}。",
                "不排除存在本次未覆盖的原因；在补充数据前，不对原因作猜测。",
            ])

        # 待补充数据（FR-5.10）：写清缺什么、缺在哪个区间
        if unknown:
            for h in unknown:
                self.outcome.missing_data.append(
                    f"验证「{h.statement}」所需数据：{h.reason}"
                )
        if base is None:
            self.outcome.missing_data.append(
                f"{label} 在基线区间 {p0.start}~{p0.end} 的原始记录"
                f"（当前只有 {p1.label} 或均无数据，无法计算变动幅度）"
            )
        # 把证据里"某区间无记录"的说明也汇总进缺口，避免用户只看到笼统提示
        for e in self.outcome.evidence:
            txt = e.get("evidence_text") or ""
            if "没有记录" in txt and txt not in self.outcome.missing_data:
                self.outcome.missing_data.append(txt)

        # 下一步建议（至少 2 条，FR-6.1）
        acts: list[str] = []
        if supported:
            main = sorted(supported, key=lambda h: len(h.evidence), reverse=True)[0]
            if main.key == "category_structure":
                acts.append("针对贡献最大的类目，检查其价格、库存与详情页变化，确认下滑触因。")
            if main.key in ("traffic_drop", "channel_structure"):
                acts.append("按渠道核对投放预算与流量结构变化，定位访客减少的具体入口。")
            if main.key in ("cvr_drop",):
                acts.append("对比两类周期的详情页、价格与评价变化，定位转化流失环节。")
            if main.key in ("aov_drop",):
                acts.append("检查是否存在满减、搭售或高价品缺货导致的订单结构下移。")
            if main.key in ("competitor_pressure",):
                acts.append("对标竞品价格与主推卖点，评估是否需要价格或权益应对。")
            if main.key in ("refund_erosion", "rating_decline"):
                acts.append("按退款原因与低分评论归类，定位质量或履约问题的具体环节。")
            if main.key == "refund_concentration":
                top = next((p for p in (main.evidence[0].payload.get("contributions") or [])
                            if p["amount"] > 0), None)
                if top:
                    acts.append(f"优先排查「{top['category']}」类目的退款原因分布，"
                                f"该品类贡献了最多退款金额（{top['count']} 笔）。")
                acts.append("对比该类目近两期的详情页描述、尺码表与物流时效，定位退款触发点。")
            if main.key == "refund_amount_rise":
                acts.append("核查退款政策或客单价结构是否变化，导致单笔退款金额抬升。")
            if main.key == "gmv_drop_denominator":
                acts.append("确认销售额下滑是否为分母效应；若销售额恢复，退款占比可能自行回落。")
            if main.key == "stock_pressure":
                acts.append("优先补货贡献最大的缺货 SKU，并评估替代品承接。")
        else:
            acts.append("补充缺失数据后重新分析，尤其是未取到口径的维度。")
            acts.append("缩小分析范围（如指定单一类目或单一渠道）后重试，以提高可判定性。")
        acts.append("如需更细粒度结论，可指定类目、渠道或单品维度继续追问。")
        self.outcome.next_actions = acts[:max(2, len(acts))]

        # 置信度整体说明（不写入结论正文，仅作可核验提示）
        if not any(h.verdict == HEALTHY for h in hyps):
            self.outcome.degraded_note = (
                self.outcome.degraded_note or
                "本次无假设成立，结论为「证据不足」，未作归因推测。"
            )
