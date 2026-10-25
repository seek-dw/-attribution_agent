"""AC-19 防退化 + AC-9/10 澄清与缺口 + AC-5 六部分结果。"""

from __future__ import annotations

import pytest

from app.analysis.engine import AnalysisEngine, parse_question
from app.db import biz_engine, init_engines


@pytest.fixture(scope="module")
def eng(biz):
    init_engines()
    e = biz_engine()
    assert e is not None
    return e


def _run(eng, q):
    return AnalysisEngine(eng, lambda k, p: None).run(q)


# --------------------------------------------------- AC-19 防退化：≥2 个假设
def test_ac19_causal_produces_at_least_two_hypotheses(eng):
    out = _run(eng, "为什么 2024-06 的销售额下滑了？").as_result()
    assert len(out["hypotheses"]) >= 2, "因果类问题必须拆出至少 2 个假设"
    for h in out["hypotheses"]:
        assert h["statement"], "假设必须有命题"
        assert h["criterion"], "假设必须有判定条件"
        assert h["verdict"] in ("成立", "不成立", "证据不足")


def test_ac19_refuted_hypotheses_have_reason(eng):
    out = _run(eng, "为什么 2024-06 的销售额下滑了？").as_result()
    refuted = [h for h in out["hypotheses"] if h["verdict"] == "不成立"]
    assert refuted, "该数据集应至少有一个假设被否定"
    for h in refuted:
        assert h["reason"], "被否定的假设必须写明否定依据"
        assert h["evidence"], "被否定的假设也必须有取证记录"


def test_ac19_verdict_not_all_same(eng):
    """如果所有假设同判，说明判定逻辑不是基于数据。"""
    out = _run(eng, "为什么 2024-06 的销售额下滑了？").as_result()
    verdicts = {h["verdict"] for h in out["hypotheses"]}
    assert len(verdicts) >= 2, f"判定结果应分化，实际 {verdicts}"


# --------------------------------------------------- AC-9 澄清
def test_ac9_missing_period_triggers_clarification(eng):
    out = _run(eng, "销售额怎么样？").as_result()
    assert out["needs_clarification"] is True
    assert "时间范围" in out["clarify_question"]


def test_ac9_missing_dimension_triggers_clarification(eng):
    out = _run(eng, "为什么 2024-06 变差了？").as_result()
    assert out["needs_clarification"] is True
    assert "指标" in out["clarify_question"]


def test_ac9_no_default_filling(eng):
    """缺信息时不得用默认值静默填充后继续（FR-5.6）。"""
    out = _run(eng, "为什么下滑了").as_result()
    assert out["needs_clarification"] is True
    assert out["hypotheses"] == []
    assert out["key_metrics"] == []


# --------------------------------------------------- AC-10 缺口声明
def test_ac10_out_of_range_reports_coverage(eng):
    out = _run(eng, "为什么 2019-01 的销售额下滑了？").as_result()
    assert "不在业务数据的覆盖区间内" in out["conclusion_text"]
    assert out["missing_data_text"].strip(), "必须声明缺什么数据"


def test_ac10_evidence_all_traceable(eng):
    """每条证据必须有来源名（FR-6.3）。"""
    out = _run(eng, "为什么 2024-06 的销售额下滑了？").as_result()
    assert out["evidence_list"], "应有证据"
    for e in out["evidence_list"]:
        assert e["source_name"], f"证据缺少来源: {e}"
        assert e["source_type"] in ("database", "attachment", "file")
        assert e["confidence"] in ("高", "中", "低")


def test_ac10_conclusion_reasons_have_evidence(eng):
    """结论中提到的每个成立原因，都要在假设列表里有对应证据（FR-6.4）。"""
    out = _run(eng, "为什么 2024-06 的销售额下滑了？").as_result()
    supported = [h for h in out["hypotheses"] if h["verdict"] == "成立"]
    for h in supported:
        assert h["evidence"], f"成立的原因必须带证据: {h['statement']}"


# --------------------------------------------------- AC-5 六部分结果
def test_ac5_six_sections_present(eng):
    out = _run(eng, "为什么 2024-06 的销售额下滑了？").as_result()
    assert out["problem_definition"].strip()
    assert out["key_metrics"], "关键指标不能为空"
    assert out["evidence_list"]
    assert out["conclusion_text"].strip()
    assert out["next_action_text"].strip()

    acts = [x for x in out["next_action_text"].split("\n") if x.strip()]
    assert len(acts) >= 2, f"下一步建议至少 2 条，实际 {len(acts)}"

    for m in out["key_metrics"]:
        assert "metric_name" in m and "metric_value" in m
        assert "metric_unit" in m and "metric_period" in m
        assert m["definition"], "指标必须带口径说明（FR-6.2）"


def test_ac5_keys_have_definitions(eng):
    out = _run(eng, "2024-06 的销售额").as_result()
    for m in out["key_metrics"]:
        assert m.get("definition"), f"指标 {m['metric_name']} 缺口径"


# --------------------------------------------------- 非因果不归因
def test_non_causal_is_not_attribution(eng):
    out = _run(eng, "2024-06 的销售额").as_result()
    assert out["hypotheses"] == [], "取数类问题不应产生归因假设"
    assert "不是归因" in out["conclusion_text"]


# --------------------------------------------------- 无显著变化不硬凑
def test_no_significant_change_no_attribution(eng):
    out = _run(eng, "为什么 2024-06 的退款变多了").as_result()
    if "未超过显著阈值" in out["conclusion_text"]:
        assert out["hypotheses"] == [], "无显著变化时不应生成归因假设"


# --------------------------------------------------- 问题解析
@pytest.mark.parametrize("q,dim,causal", [
    ("为什么 2024-06 的销售额下滑了", "gmv", True),
    ("为什么订单量下降了 2024-05", "orders", True),
    ("转化率怎么样 2024-06", "cvr", False),
    ("为什么 2024-06 的客单价降了", "aov", True),
])
def test_parse_question(q, dim, causal):
    p = parse_question(q)
    assert p.dimension == dim
    assert p.is_causal is causal
