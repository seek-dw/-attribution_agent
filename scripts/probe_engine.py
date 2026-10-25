"""直接驱动分析引擎，看真实产出（不走 HTTP，排除接口层干扰）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.analysis.engine import AnalysisEngine  # noqa: E402
from app.db import biz_engine, init_engines  # noqa: E402

init_engines()
eng = biz_engine()
if eng is None:
    print("业务库不存在，先跑 scripts/init_db.py --force")
    raise SystemExit(1)

CASES = [
    "为什么 2024-06 的销售额下滑了？",
    "为什么上个月销售额跌了？",          # 无明确月份 → 应澄清
    "销售额怎么样？",                     # 缺时间 → 应澄清
    "为什么 2024-06 的订单量下降",        # 换维度
    "为什么 2024-06 的退款变多了",        # 退款维度
    "2024-06 的销售额",                   # 非因果，只报数
]

for q in CASES:
    print("=" * 78)
    print(f"问题：{q}")
    engine = AnalysisEngine(eng, lambda k, p: None)
    out = engine.run(q)
    d = out.as_result()
    if d["needs_clarification"]:
        print(f"  [澄清] {d['clarify_question']}")
        print(f"  [缺口] {d['missing_data_text']}")
        continue

    print(f"  问题定义：{d['problem_definition']}")
    print("  关键指标：")
    for m in d["key_metrics"]:
        print(f"    - {m['metric_name']}: {m['metric_value']}{m['metric_unit']}"
              f"  基线={m.get('baseline_value')}  变动={m.get('delta_pct')}%")
    print(f"  假设数：{len(d['hypotheses'])}")
    for h in d["hypotheses"]:
        mark = {"成立": "✓", "不成立": "✗", "证据不足": "?"}.get(h["verdict"], "·")
        print(f"    {mark} [{h['verdict']}] {h['statement']}")
        print(f"        依据：{h['reason']}")
    print(f"  证据数：{len(d['evidence_list'])}")
    print(f"  结论：{d['conclusion_text']}")
    print(f"  待补充：{d['missing_data_text'] or '无'}")
    print(f"  下一步：{d['next_action_text']}")
    if d.get("degraded_note"):
        print(f"  [说明] {d['degraded_note']}")
    print(f"  步骤数：{d['step_count']}")
