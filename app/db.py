"""数据层：应用库 + 只读业务库。

关键约束落在数据层而非应用层 if（NFR-6 / NFR-7）：
- 同一会话只允许一个 running 任务 → 部分唯一索引
- 消息顺序号唯一 → 唯一索引
- WebSocket 令牌一次性 → 条件 UPDATE + rowcount
- 业务库以只读方式打开 → mode=ro URI
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
    create_engine, event, text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from . import config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_user_id: Mapped[str] = mapped_column(String(64), unique=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(256), default="")
    role: Mapped[str] = mapped_column(String(32), default="analyst")  # analyst | admin
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))          # user | assistant | system | tool
    message_type: Mapped[str] = mapped_column(String(24), default="text")
    content: Mapped[str] = mapped_column(Text, default="")
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    seq_no: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        # FR-2.6：顺序号在同一会话内唯一，防重复/乱序
        Index("uq_message_seq", "conversation_id", "seq_no", unique=True),
    )


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    file_name: Mapped[str] = mapped_column(String(256))
    file_path: Mapped[str] = mapped_column(String(512))
    file_type: Mapped[str] = mapped_column(String(64), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    parse_status: Mapped[str] = mapped_column(String(16), default="pending")
    parse_error: Mapped[str] = mapped_column(Text, default="")
    text_excerpt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    input_text: Mapped[str] = mapped_column(Text, default="")
    task_status: Mapped[str] = mapped_column(String(16), default="queued")
    current_step: Mapped[str] = mapped_column(String(64), default="")
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        # FR-2.4 / NFR-7：同一会话同时只允许一个 running 任务，由数据库强制
        Index(
            "uq_running_task_per_conversation",
            "conversation_id",
            unique=True,
            sqlite_where=text("task_status = 'running'"),
        ),
    )


class AnalysisResult(Base):
    __tablename__ = "analysis_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("analysis_tasks.id"), unique=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    problem_definition: Mapped[str] = mapped_column(Text, default="")
    key_metrics_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_list_json: Mapped[str] = mapped_column(Text, default="[]")
    conclusion_text: Mapped[str] = mapped_column(Text, default="")
    missing_data_text: Mapped[str] = mapped_column(Text, default="")
    next_action_text: Mapped[str] = mapped_column(Text, default="")
    hypotheses_json: Mapped[str] = mapped_column(Text, default="[]")   # FR-5.11
    tool_plan_json: Mapped[str] = mapped_column(Text, default="{}")
    artifacts_json: Mapped[str] = mapped_column(Text, default="[]")
    degraded_note: Mapped[str] = mapped_column(Text, default="")
    result_markdown: Mapped[str] = mapped_column(Text, default="")
    result_file_path: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class DataSource(Base):
    """数据源连接（2.1.2：管理员管理数据源连接）。

    设计要点：
    - 只存路径/URI，不存凭据明文
    - 支持 sqlite 文件与只读 URI 两种模式
    - 有 active 标记，同一时间只有一个是默认分析库
    """
    __tablename__ = "data_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(24), default="sqlite")  # sqlite
    path: Mapped[str] = mapped_column(String(512))
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # ok|error|unknown
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ContextSummary(Base):
    __tablename__ = "context_summaries"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    start_seq_no: Mapped[int] = mapped_column(Integer)
    end_seq_no: Mapped[int] = mapped_column(Integer)
    summary_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WebsocketToken(Base):
    __tablename__ = "websocket_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    token: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SystemConfig(Base):
    __tablename__ = "system_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    config_key: Mapped[str] = mapped_column(String(64), unique=True)
    config_value: Mapped[str] = mapped_column(Text, default="")
    config_group: Mapped[str] = mapped_column(String(32), default="general")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class TaskLog(Base):
    __tablename__ = "task_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_tasks.id"), nullable=True, index=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_level: Mapped[str] = mapped_column(String(16), default="info")
    log_type: Mapped[str] = mapped_column(String(32), default="step")
    log_content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ToolInvocation(Base):
    """FR-6.3：每条证据必须能追溯到一次具体工具调用。"""
    __tablename__ = "tool_invocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("analysis_tasks.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|success|failed|rejected
    result_summary: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error_message: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class RejectionCounter(Base):
    """NFR-2：所有拒绝路径必须有计数器。"""
    __tablename__ = "rejection_counters"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    last_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SessionState(Base):
    """澄清中断后的断点恢复（FR-5.7）。"""
    __tablename__ = "analysis_session_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), unique=True)
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    pending_question: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# --------------------------------------------------------------------------
# engine / session
# --------------------------------------------------------------------------
_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
_biz_engine: Engine | None = None
_biz_engine_key: str = ""


def _enable_fk(dbapi_conn, _rec) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def _make_biz_engine(path: str, read_only: bool) -> Engine:
    if read_only:
        uri = f"sqlite+pysqlite:///file:{Path(path).as_posix()}?mode=ro&uri=true"
    else:
        uri = f"sqlite+pysqlite:///{Path(path).as_posix()}"
    return create_engine(
        uri,
        connect_args={"check_same_thread": False},
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        future=True,
    )


def init_engines() -> None:
    global _engine, _SessionLocal, _biz_engine, _biz_engine_key
    config.ensure_dirs()

    # SQLite 是单写锁，但读可以并发；连接池过小会在"分析任务 + WS + 页面请求"
    # 同时持有会话时耗尽（曾观测到 QueuePool limit of size 5 报错）。
    # max_overflow 放宽 + pool_timeout 缩短，让拿不到连接时快速失败而不是挂 30 秒。
    _engine = create_engine(
        f"sqlite+pysqlite:///{config.APP_DB.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 15},
        pool_size=20,
        max_overflow=40,
        pool_timeout=10,
        pool_pre_ping=True,
        future=True,
    )
    event.listen(_engine, "connect", _enable_fk)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)

    # 业务库：先回落默认只读文件，随后由 resolve_biz_source() 按数据源表切换
    _biz_engine = None
    _biz_engine_key = ""
    if config.BIZ_DB.exists():
        _biz_engine = _make_biz_engine(str(config.BIZ_DB), True)
        _biz_engine_key = f"{config.BIZ_DB}|ro"


def create_all() -> None:
    Base.metadata.create_all(_engine)
    _migrate_add_columns()


# 轻量迁移：create_all 不会给已存在的表补列。
# 新增字段时在这里登记，避免旧库升级后 500。
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("analysis_results", "tool_plan_json", "TEXT DEFAULT '{}'"),
    ("analysis_results", "artifacts_json", "TEXT DEFAULT '[]'"),
    ("analysis_results", "degraded_note", "TEXT DEFAULT ''"),
    ("attachments", "parse_error", "TEXT DEFAULT ''"),
    ("attachments", "text_excerpt", "TEXT DEFAULT ''"),
    ("users", "password_hash", "TEXT DEFAULT ''"),
]


def _migrate_add_columns() -> None:
    from sqlalchemy import inspect

    insp = inspect(_engine)
    for table, column, ddl in _ADDED_COLUMNS:
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        if column in existing:
            continue
        with _engine.begin() as conn:
            try:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            except Exception:  # noqa: BLE001
                # 加列失败不阻断启动，但要在日志里可见（不静默）
                print(f"[migrate] 未能为 {table}.{column} 加列，请检查现有库结构")


def biz_engine() -> Engine | None:
    """取当前生效的业务库引擎。

    优先用 is_active 的数据源；没有则回落默认 business.db。
    切换后第一次调用会重建引擎，并关闭旧引擎释放连接。
    """
    global _biz_engine, _biz_engine_key

    src = None
    try:
        s = get_session()
        try:
            src = s.query(DataSource).filter_by(is_active=True).one_or_none()
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        # 数据源表还不存在（首次启动）
        src = None

    if src is None:
        path, ro = str(config.BIZ_DB), True
    else:
        path, ro = src.path, bool(src.read_only)

    key = f"{path}|{'ro' if ro else 'rw'}"
    if _biz_engine is not None and key == _biz_engine_key:
        return _biz_engine

    if not Path(path).exists():
        return None

    old = _biz_engine
    _biz_engine = _make_biz_engine(path, ro)
    _biz_engine_key = key
    if old is not None:
        old.dispose()
    return _biz_engine


def session_scope() -> Iterator[Session]:
    assert _SessionLocal is not None
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Session:
    """直接取会话（需调用方自行 close）。"""
    assert _SessionLocal is not None
    return _SessionLocal()


def session_dep() -> Iterator[Session]:
    """FastAPI 依赖：请求结束必定归还连接，避免连接池耗尽。

    此前把 get_session 直接当依赖用，会话从不 close，
    实测在"分析任务 + WebSocket + 页面请求"并发时触发
    QueuePool limit of size 5 reached。这里改成生成器依赖。
    """
    assert _SessionLocal is not None
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def bump_rejection(s: Session, code: str) -> None:
    row = s.query(RejectionCounter).filter_by(code=code).one_or_none()
    if row is None:
        s.add(RejectionCounter(code=code, count=1, last_at=_now()))
    else:
        row.count += 1
        row.last_at = _now()
