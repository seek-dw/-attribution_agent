"""分析意图路由：判断一个问题该用哪些工具取证。

原设计要求"分析能力包含数据库查询、文件读写、文本检索、命令执行"。
本模块让引擎真正按问题需要选择工具，而不是只用 db_query。

选工具的依据是问题里出现的线索：
- 提到附件/文件/上传/报告里的内容 → text_search / file_read
- 提到明细/导出/算一下 → 可能用 file_write 落中间结果
- 提到校验/统计口径 → 可能用 command_run 跑脚本
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ToolPlan:
    """一次分析需要用到哪些工具。"""
    need_text_search: bool = False
    need_file_read: bool = False
    need_file_write: bool = False
    need_command: bool = False
    keywords: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, bool]:
        return {
            "text_search": self.need_text_search,
            "file_read": self.need_file_read,
            "file_write": self.need_file_write,
            "command": self.need_command,
        }


_ATTACHMENT_HINTS = (
    "附件", "上传", "文件", "文档", "表格里", "csv 里", "报告里",
    "资料里", "那个表", "我给的",
)

_DETAIL_HINTS = (
    "明细", "逐条", "列表", "全部列出来", "具体是哪些", "哪些订单",
)

_EXPORT_HINTS = (
    "导出", "存成", "写成文件", "落个文件", "生成文件",
)

_COMPUTE_HINTS = (
    "算一下", "校验", "验证一下", "统计一下", "跑一下", "脚本",
)


def plan_tools(question: str, has_attachments: bool) -> ToolPlan:
    """按问题内容决定工具组合。"""
    q = question or ""
    low = q.lower()
    p = ToolPlan()

    if has_attachments and any(h in q for h in _ATTACHMENT_HINTS):
        p.need_text_search = True
        p.need_file_read = any(h in q for h in ("文件", "文档", "报告里", "资料里"))

    if any(h in q for h in _EXPORT_HINTS):
        p.need_file_write = True

    if any(h in q for h in _COMPUTE_HINTS):
        p.need_command = True

    # 抽出用于检索的关键词：中文分词太重，取问题中的实义片段
    if p.need_text_search:
        p.keywords = _extract_keywords(q)

    return p


_STOP = {"为什么", "怎么", "什么", "如何", "能不能", "是不是", "有没有",
         "一下", "帮我", "看一下", "分析", "原因", "下滑", "下降", "上升",
         "上涨", "的", "了", "吗", "呢", "我", "给的", "里"}


def _extract_keywords(question: str, limit: int = 4) -> list[str]:
    """从问题里提取可用于文本检索的关键词。

    策略：按标点与虚词切分，取长度≥2 的片段；同时保留数字与英文词，
    因为"渠道 A""SKU-12"这类标识符往往是附件里最有区分度的检索锚点。
    """
    cleaned = re.sub(r"[，。！？、；：,.!?;:\"“”'‘’（）()\[\]【】]", " ", question)
    tokens: list[str] = []
    for chunk in cleaned.split():
        if chunk in _STOP or len(chunk) < 2:
            continue
        if re.fullmatch(r"[0-9A-Za-z\-_]{2,}", chunk):
            tokens.append(chunk)
            continue
        # 中文片段：切掉常见虚词
        for w in ("为什么", "帮我", "看一下", "分析", "一下", "的", "了", "吗", "里"):
            chunk = chunk.replace(w, " ")
        for piece in chunk.split():
            if len(piece) >= 2 and piece not in _STOP:
                tokens.append(piece)

    seen: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.append(t)
    return seen[:limit]
