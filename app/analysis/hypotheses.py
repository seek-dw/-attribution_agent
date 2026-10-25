"""假设引擎：把模糊因果问题拆成可判定假设，逐个取证判定（FR-5.1 / FR-5.11）。

设计要点：
- 假设不是预埋的，而是按问题类型从可用维度生成
- 每个假设有明确的判定条件（predicate），不靠模型断言
- 判定三态：成立 / 不成立 / 证据不足（不足即进入待补充数据，FR-5.10）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

HEALTHY = "成立"
REFUTED = "不成立"
UNKNOWN = "证据不足"


@dataclass
class EvidenceItem:
    source_type: str
    source_name: str
    evidence_text: str
    related_metric: str
    confidence: str
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = {
            "source_type": self.source_type,
            "source_name": self.source_name,
            "evidence_text": self.evidence_text,
            "related_metric": self.related_metric,
            "confidence": self.confidence,
        }
        if self.payload:
            d["payload"] = self.payload
        return d


@dataclass
class Hypothesis:
    key: str
    statement: str                # 假设命题
    criterion: str                # 判定条件（人可读）
    verdict: str = UNKNOWN
    reason: str = ""              # 判定依据
    evidence: list[EvidenceItem] = field(default_factory=list)
    supports_conclusion: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "statement": self.statement,
            "criterion": self.criterion,
            "verdict": self.verdict,
            "reason": self.reason,
            "evidence": [e.as_dict() for e in self.evidence],
            "supports_conclusion": self.supports_conclusion,
        }


def confidence_level(sample_rows: int, has_direct_metric: bool,
                     contradicted: bool) -> str:
    """FR-6.5：置信度由可解释规则产生，不由模型随意断言。

    - 存在反驳性证据 → 低
    - 直接指标 + 样本量达标 → 高
    - 其余 → 中
    """
    if contradicted:
        return "低"
    if has_direct_metric and sample_rows >= 100:
        return "高"
    if has_direct_metric or sample_rows >= 30:
        return "中"
    return "低"


def pct_delta(cur: float, base: float) -> float | None:
    if base == 0:
        return None
    return (cur - base) / abs(base) * 100.0


def fmt_pct(v: float | None) -> str:
    return "无法计算" if v is None else f"{v:+.2f}%"
