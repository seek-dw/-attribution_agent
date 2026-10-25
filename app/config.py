"""配置：默认值 + 热更新（FR-8.1~8.4）。"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
# 允许用环境变量切换数据目录：验收脚本、演示重放可以指向独立库，
# 避免反复跑脚本把测试会话写进正式库（曾经累积 17 个重复会话）。
DATA_DIR = Path(os.environ.get("ATTR_DATA_DIR") or (BASE_DIR / "data")).resolve()
APP_DB = DATA_DIR / "app.db"
BIZ_DB = DATA_DIR / "business.db"
STORAGE_DIR = DATA_DIR / "storage"
CONFIG_FILE = DATA_DIR / "runtime_config.json"

# FR-8.4：每个配置项必须有默认值
DEFAULTS: dict[str, Any] = {
    # 工具执行边界
    "db_query_max_rows": 1000,
    "file_read_max_bytes": 2_000_000,
    "command_timeout_sec": 10,
    "command_output_max_chars": 4000,
    # 分析任务限制
    "task_max_steps": 30,
    "task_max_seconds": 600,
    # 上下文摘要阈值（FR-7.1）
    "summary_trigger_messages": 40,
    "summary_keep_recent": 10,
    # 归因判定阈值
    "delta_threshold_pct": 5.0,
    "min_rows_for_high_confidence": 100,
    # 生成来源（FR-3.5）：rule_template | llm
    "generation_source": "rule_template",
    "llm_base_url": "",
    "llm_model": "",
    "llm_api_key": "",
    # 功能开关（2.1.2：管理员启停功能开关）
    "enable_attachment": True,
    "enable_command_tool": True,
    "enable_file_tools": True,
    "enable_text_search": True,
    "enable_context_summary": True,
    "enable_export": True,
}

# 开关类配置：值必须是布尔，前端渲染成开关而不是输入框
SWITCH_KEYS: frozenset[str] = frozenset({
    "enable_attachment", "enable_command_tool", "enable_file_tools",
    "enable_text_search", "enable_context_summary", "enable_export",
})

# 开关关闭时对外说明的后果，避免"关了不知道为什么不好用"
SWITCH_EFFECTS: dict[str, str] = {
    "enable_attachment": "关闭后无法上传附件，附件不会再进入证据链",
    "enable_command_tool": "关闭后分析中的命令执行工具不可用",
    "enable_file_tools": "关闭后分析无法读写中间文件",
    "enable_text_search": "关闭后不会从附件里检索证据",
    "enable_context_summary": "关闭后长会话不再生成上下文摘要",
    "enable_export": "关闭后结果只可复制，不可导出文件",
}


def switch_on(key: str) -> bool:
    """功能开关判定。缺失或类型异常时按开启处理，并提供日志可见的降级。"""
    try:
        v = get(key)
    except KeyError:
        return True
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "off", "no", "")
    return bool(v)

# Q-5 命令白名单
COMMAND_WHITELIST: frozenset[str] = frozenset(
    {"python", "python3", "ls", "cat", "head", "grep", "wc", "findstr", "dir"}
)

_lock = threading.RLock()
_cache: dict[str, Any] = {}
# 上次生效值的快照。reload 的「变更前后」以它为基准——
# 否则 write_config 先改文件，reload 时 before 读到的已经是新值，差异永远为空。
_effective_snapshot: dict[str, Any] = {}


def _load_file() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _cache_for_keys(keys: set[str]) -> dict[str, Any]:
    """按当前生效来源取一组键的值（缓存优先，其次文件，最后默认）。"""
    file_cfg = _load_file()
    out: dict[str, Any] = {}
    for k in keys:
        if k in _cache:
            out[k] = _cache[k]
        elif k in file_cfg:
            out[k] = file_cfg[k]
        elif k in DEFAULTS:
            out[k] = DEFAULTS[k]
    return out


def get(key: str) -> Any:
    """读取配置；缺失时回落默认值（FR-8.4）。"""
    with _lock:
        if key in _cache:
            return _cache[key]
        file_cfg = _load_file()
        if key in file_cfg:
            _cache[key] = file_cfg[key]
            return _cache[key]
        if key in DEFAULTS:
            return DEFAULTS[key]
        raise KeyError(f"未定义的配置项: {key}")


def all_values() -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(_load_file())
    return merged


def reload_config(actor: str = "system") -> dict[str, Any]:
    """热更新：重读文件，清缓存（FR-8.2）。返回变更前后差异（FR-8.3）。

    比对基准是「上一次 reload 后的生效值」。首次调用时若无快照，
    以当前默认值为基准。
    """
    global _effective_snapshot
    with _lock:
        before = dict(_effective_snapshot) if _effective_snapshot else dict(DEFAULTS)

        _cache.clear()
        after = all_values()
        changed = {k: {"before": before.get(k), "after": after.get(k)}
                   for k in set(before) | set(after)
                   if before.get(k) != after.get(k)}
        _effective_snapshot = dict(after)
        return {
            "reloaded_at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "changed": changed,
            "effective": after,
        }


def write_config(updates: dict[str, Any]) -> None:
    """管理员写入配置文件（外部入口），随后由 reload 生效。"""
    with _lock:
        current = _load_file()
        current.update(updates)
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _cache.clear()


def ensure_dirs() -> None:
    for p in (DATA_DIR, STORAGE_DIR, STORAGE_DIR / "uploads",
              STORAGE_DIR / "exports", STORAGE_DIR / "workspace"):
        p.mkdir(parents=True, exist_ok=True)


def secret_key() -> bytes:
    key_file = DATA_DIR / "secret.key"
    if key_file.exists():
        return key_file.read_bytes()
    key = os.urandom(32)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(key)
    return key
