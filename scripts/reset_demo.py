"""清理验收/截图脚本产生的测试会话，只保留演示数据。

用法:
  python scripts/reset_demo.py --dry-run   # 先看会删什么
  python scripts/reset_demo.py             # 执行清理并重建演示会话
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.analysis.runner import delete_conversation_cascade  # noqa: E402
from app.db import Conversation, Message, get_session, init_engines  # noqa: E402

# 演示会话的标题特征：只有这几类是"正式演示数据"，保留
KEEP_TITLES = (
    "销售额下滑归因 · 6月",
    "退款占比上升归因 · 6月",
    "成交额取数与缺口演示",
)

# 脚本产生的噪音会话：标题含这些就删
# 注意：demo.py 用「市场表现分析（…）」「退款模式分析（…）」，
# 语义上和正式演示重复，属于验收副产品，应删除。
NOISE_TITLES = (
    "审计会话",
    "状态测试",
    "E2E 验收会话",
    "市场表现分析",
    "退款模式分析",
    "自建库归因",
    "切库后分析",
    "回落验证",
)


def classify(title: str) -> str:
    if any(k in title for k in KEEP_TITLES):
        return "keep"
    if any(k in title for k in NOISE_TITLES):
        return "noise"
    if title.startswith("分析 "):
        return "auto"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-unknown", action="store_true",
                    help="保留无法归类的会话（默认也保留）")
    args = ap.parse_args()

    init_engines()
    s = get_session()
    try:
        rows = s.query(Conversation).order_by(Conversation.id).all()
        buckets: dict[str, list[Conversation]] = {
            "keep": [], "noise": [], "auto": [], "unknown": []}
        for c in rows:
            buckets[classify(c.title)].append(c)

        print(f"数据目录: {config.DATA_DIR}")
        print(f"会话总数: {len(rows)}\n")
        for kind, label in (("keep", "保留（演示）"), ("noise", "删除（脚本噪音）"),
                            ("auto", "删除（截图新建）"), ("unknown", "保留（未归类）")):
            items = buckets[kind]
            print(f"{label}: {len(items)} 个")
            for c in items:
                n = s.query(Message).filter_by(conversation_id=c.id).count()
                print(f"   #{c.id:<3} {c.title[:34]:<36} 消息 {n}")

        targets = buckets["noise"] + buckets["auto"]
        if not targets:
            print("\n无需清理。")
            return 0

        if args.dry_run:
            print(f"\n[试运行] 将删除 {len(targets)} 个会话，未实际执行。")
            return 0

        for c in targets:
            delete_conversation_cascade(s, c)
        s.commit()
        print(f"\n已删除 {len(targets)} 个会话。")

        remain = s.query(Conversation).count()
        print(f"剩余会话: {remain} 个")
        for c in s.query(Conversation).order_by(Conversation.id).all():
            print(f"   #{c.id:<3} {c.title}")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
