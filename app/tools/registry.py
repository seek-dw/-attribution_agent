"""工具层：五类工具，全部带执行边界（FR-5.2~5.5）。

统一约定：
- 每个工具返回 ToolOutcome，包含 status（success/failed/rejected）
- 拒绝一律 bump_rejection 计数（NFR-2）
- 失败必须可归因（NFR-8），不得静默
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import config
from ..db import bump_rejection, biz_engine

# FR-5.3：只读语句校验，不依赖数据库权限作为唯一防线
_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|replace|attach|detach|pragma|vacuum|reindex)\b",
    re.IGNORECASE,
)


@dataclass
class ToolOutcome:
    tool: str
    status: str                      # success | failed | rejected
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0
    citations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "success"


def _reject(s: Session, tool: str, code: str, msg: str) -> ToolOutcome:
    bump_rejection(s, code)
    return ToolOutcome(tool=tool, status="rejected", error=msg, summary=msg)


# --------------------------------------------------------------------------
# 1. 数据库查询（只读，FR-5.3）
# --------------------------------------------------------------------------
def db_query(s: Session, sql: str, params: dict[str, Any] | None = None) -> ToolOutcome:
    started = time.perf_counter()
    tool = "db_query"

    if not sql or not sql.strip():
        return _reject(s, tool, "db_query.empty_sql", "SQL 为空")

    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        return _reject(s, tool, "db_query.multi_statement", "不允许一次提交多条语句")
    if _WRITE_KEYWORDS.search(stripped):
        return _reject(
            s, tool, "db_query.non_readonly",
            "只允许只读查询；检测到写入或结构变更关键字",
        )
    if not re.match(r"^\s*(select|with)\b", stripped, re.IGNORECASE):
        return _reject(s, tool, "db_query.not_select", "只允许 SELECT 或 WITH 开头")

    engine = biz_engine()
    if engine is None:
        return ToolOutcome(tool=tool, status="failed", error="业务库未初始化", summary="业务库不可用")

    max_rows = int(config.get("db_query_max_rows"))
    try:
        with engine.connect() as conn:
            result = conn.execute(text(stripped), params or {})
            rows = result.fetchmany(max_rows + 1)
            cols = list(result.keys())
    except Exception as exc:  # noqa: BLE001
        return ToolOutcome(
            tool=tool, status="failed", error=str(exc)[:500],
            summary=f"查询失败: {str(exc)[:200]}",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]
    records = [dict(zip(cols, r)) for r in rows]
    dur = int((time.perf_counter() - started) * 1000)

    return ToolOutcome(
        tool=tool, status="success",
        summary=f"返回 {len(records)} 行" + ("（已截断）" if truncated else ""),
        data={"columns": cols, "rows": records, "truncated": truncated, "sql": stripped},
        duration_ms=dur,
        citations=[{
            "source_type": "database",
            "source_name": f"Query#{int(time.time() * 1000) % 100000}",
            "sql": stripped,
        }],
    )


# --------------------------------------------------------------------------
# 2/3. 文件读 / 写（限 workspace，FR-5.5）
# --------------------------------------------------------------------------
def _safe_path(base: Path, relative: str) -> Path | None:
    """路径穿越防护：拒绝 ..、绝对路径、逃逸出 base。"""
    if not relative:
        return None
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    return candidate


def _user_space(user_id: int, conversation_id: int, kind: str) -> Path:
    return (config.STORAGE_DIR / kind / str(user_id) / str(conversation_id)).resolve()


def file_write(s: Session, user_id: int, conversation_id: int,
               path: str, content: str) -> ToolOutcome:
    started = time.perf_counter()
    tool = "file_write"
    base = _user_space(user_id, conversation_id, "workspace")
    base.mkdir(parents=True, exist_ok=True)
    target = _safe_path(base, path)
    if target is None:
        return _reject(s, tool, "file_write.path_escape", f"路径越界或非法: {path}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolOutcome(tool=tool, status="failed", error=str(exc), summary="写文件失败")
    size = target.stat().st_size
    return ToolOutcome(
        tool=tool, status="success",
        summary=f"已写入 {size} 字节",
        data={"path": str(target.relative_to(base)), "size": size},
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def file_read(s: Session, user_id: int, conversation_id: int, path: str) -> ToolOutcome:
    started = time.perf_counter()
    tool = "file_read"
    base = _user_space(user_id, conversation_id, "workspace")
    target = _safe_path(base, path)
    if target is None:
        return _reject(s, tool, "file_read.path_escape", f"路径越界或非法: {path}")
    if not target.exists() or not target.is_file():
        return ToolOutcome(tool=tool, status="failed", error="文件不存在", summary=f"文件不存在: {path}")
    limit = int(config.get("file_read_max_bytes"))
    size = target.stat().st_size
    if size > limit:
        return _reject(s, tool, "file_read.too_large", f"文件 {size} 字节超过上限 {limit}")
    content = target.read_text(encoding="utf-8", errors="replace")
    return ToolOutcome(
        tool=tool, status="success",
        summary=f"读取 {size} 字节",
        data={"path": path, "content": content, "size": size},
        duration_ms=int((time.perf_counter() - started) * 1000),
        citations=[{"source_type": "file", "source_name": path}],
    )


# --------------------------------------------------------------------------
# 4. 文本检索（限附件与结果）
# --------------------------------------------------------------------------
def text_search(s: Session, user_id: int, conversation_id: int,
                keyword: str, limit: int = 20) -> ToolOutcome:
    started = time.perf_counter()
    tool = "text_search"
    if not keyword or not keyword.strip():
        return _reject(s, tool, "text_search.empty_keyword", "关键词为空")

    from ..db import Attachment

    rows = (
        s.query(Attachment)
        .filter(Attachment.conversation_id == conversation_id)
        .filter(Attachment.parse_status == "success")
        .all()
    )
    hits: list[dict[str, Any]] = []
    for att in rows:
        body = att.text_excerpt or ""
        idx = body.find(keyword)
        if idx < 0:
            continue
        start = max(0, idx - 60)
        hits.append({
            "source_type": "attachment",
            "source_name": att.file_name,
            "snippet": body[start:start + 220],
            "offset": idx,
        })
        if len(hits) >= limit:
            break

    return ToolOutcome(
        tool=tool, status="success",
        summary=f"命中 {len(hits)} 处",
        data={"keyword": keyword, "hits": hits},
        duration_ms=int((time.perf_counter() - started) * 1000),
        citations=[{"source_type": h["source_type"], "source_name": h["source_name"]} for h in hits],
    )


# --------------------------------------------------------------------------
# 5. 命令执行（白名单 + 超时 + 输出截断，FR-5.4）
# --------------------------------------------------------------------------
def command_run(s: Session, user_id: int, conversation_id: int, command: str) -> ToolOutcome:
    started = time.perf_counter()
    tool = "command_run"
    if not command or not command.strip():
        return _reject(s, tool, "command.empty", "命令为空")

    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        return _reject(s, tool, "command.unparsable", f"命令无法解析: {exc}")
    if not argv:
        return _reject(s, tool, "command.empty", "命令为空")

    head = Path(argv[0]).name.lower()
    if head.endswith(".exe"):
        head = head[:-4]
    if head not in config.COMMAND_WHITELIST:
        return _reject(
            s, tool, "command.not_whitelisted",
            f"命令 {argv[0]} 不在白名单内；允许: {sorted(config.COMMAND_WHITELIST)}",
        )

    # Windows 上 PATH 里的 python 可能指向 WindowsApps 假入口：
    # 退出码 0、输出为空，静默失败。解析到真实解释器，否则显式报错。
    if head in ("python", "python3"):
        real = sys.executable
        if not real or not Path(real).exists():
            return ToolOutcome(
                tool=tool, status="failed",
                error="找不到可用的 Python 解释器",
                summary="解释器不可用，命令未执行",
            )
        argv[0] = real

    base = _user_space(user_id, conversation_id, "workspace")
    base.mkdir(parents=True, exist_ok=True)

    # 仅对形如路径的参数做越界检查；跳过 -c 后的代码文本
    skip_next = False
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in ("-c", "-m"):
            skip_next = True
            continue
        if arg in ("..",) or "\\" in arg or "/" in arg:
            if _safe_path(base, arg.lstrip("/\\")) is None:
                return _reject(s, tool, "command.path_escape", f"参数路径越界: {arg}")

    timeout = int(config.get("command_timeout_sec"))
    try:
        proc = subprocess.run(
            argv, cwd=str(base), capture_output=True, text=True,
            timeout=timeout, shell=False,
        )
    except subprocess.TimeoutExpired:
        bump_rejection(s, "command.timeout")
        return ToolOutcome(
            tool=tool, status="failed",
            error=f"命令超过 {timeout} 秒被终止",
            summary="命令超时被终止",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except OSError as exc:
        return ToolOutcome(tool=tool, status="failed", error=str(exc), summary="命令无法启动")

    # 静默失败识别：退出码 0 但既无输出也无错误，且命令本应有输出
    if proc.returncode == 0 and not proc.stdout and not proc.stderr:
        return ToolOutcome(
            tool=tool, status="failed",
            error="命令返回 0 但没有任何输出，疑似解释器或命令为假入口",
            summary="命令无输出，按失败处理（不当作成功）",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    limit = int(config.get("command_output_max_chars"))
    out = (proc.stdout or "") + (proc.stderr or "")
    truncated = len(out) > limit
    if truncated:
        out = out[:limit] + f"\n...[已截断，原始 {len(out)} 字符]"

    return ToolOutcome(
        tool=tool, status="success" if proc.returncode == 0 else "failed",
        summary=f"退出码 {proc.returncode}，输出 {len(out)} 字符",
        data={"exit_code": proc.returncode, "output": out, "truncated": truncated},
        error="" if proc.returncode == 0 else f"退出码 {proc.returncode}",
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


# --------------------------------------------------------------------------
# 6. 结果文件生成
# --------------------------------------------------------------------------
def result_export(s: Session, user_id: int, conversation_id: int,
                  filename: str, content: str) -> ToolOutcome:
    started = time.perf_counter()
    tool = "result_export"
    base = _user_space(user_id, conversation_id, "exports")
    base.mkdir(parents=True, exist_ok=True)
    target = _safe_path(base, filename)
    if target is None:
        return _reject(s, tool, "result_export.path_escape", f"文件名非法: {filename}")
    target.write_text(content, encoding="utf-8")
    return ToolOutcome(
        tool=tool, status="success",
        summary=f"已导出 {target.name}",
        data={"path": str(target), "filename": target.name,
              "size": target.stat().st_size},
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
