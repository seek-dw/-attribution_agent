"""清理未使用的导入（上传 GitHub 前整理代码）。

只删除确认未被引用的 import，改完立即用 ast 复查。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 人工确认过的清理清单：文件 → 要删的名字
TARGETS: dict[str, list[str]] = {
    "app/api.py": ["FastAPI", "StaticFiles", "get_session"],
    "app/db.py": ["Any", "Float"],
    "app/analysis/engine.py": ["metric_value"],
    "app/analysis/hypotheses.py": ["Callable"],
    "app/analysis/metrics.py": ["datetime"],
    "app/analysis/runner.py": ["IntegrityError", "Path", "Period",
                               "bump_rejection", "parse_question"],
    "app/analysis/sample_data.py": ["calendar"],
    "app/tools/registry.py": ["json"],
}


def unused_names(path: Path) -> set[str]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imported.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name != "*":
                    imported.add(a.asname or a.name)
    used: set[str] = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            v = n
            while isinstance(v, ast.Attribute):
                v = v.value
            if isinstance(v, ast.Name):
                used.add(v.id)
    return imported - used - {"annotations"}


def strip_name(src: str, name: str) -> str:
    """从 from ... import a, b, c 里删掉一个名字；若只剩一个则整行删除。"""
    lines = src.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(\s*from\s+[\w.]+\s+import\s+)(.+?)(\s*(?:#.*)?)$", line)
        if not m:
            out.append(line)
            i += 1
            continue
        head, names_part, tail = m.group(1), m.group(2), m.group(3)
        # 括号跨行的情况单独处理
        if names_part.strip().startswith("(") or "(" in names_part:
            out.append(line)
            i += 1
            continue
        names = [x.strip() for x in names_part.split(",") if x.strip()]
        if name not in names:
            out.append(line)
            i += 1
            continue
        rest = [x for x in names if x != name]
        if not rest:
            # 整行删除（保留换行结构）
            i += 1
            continue
        out.append(f"{head}{', '.join(rest)}{tail}")
        i += 1
    return "".join(out)


def strip_plain_import(src: str, name: str) -> str:
    """删除 `import json` 这种单名导入。"""
    return re.sub(rf"^import {re.escape(name)}\s*$\n?", "", src, flags=re.MULTILINE)


def main() -> int:
    for rel, names in TARGETS.items():
        p = ROOT / rel
        src = p.read_text(encoding="utf-8")
        before = unused_names(p)
        for name in names:
            if name not in before:
                print(f"  跳过 {rel}: {name}（已不在未使用列表）")
                continue
            src = strip_plain_import(src, name)
            src = strip_name(src, name)
        p.write_text(src, encoding="utf-8")
        after = unused_names(p)
        print(f"  {rel}: {sorted(before)} → {sorted(after)}")

    print("\n复查全部：")
    remain = []
    for p in (ROOT / "app").rglob("*.py"):
        u = unused_names(p)
        if u:
            remain.append((p.relative_to(ROOT), sorted(u)))
    if remain:
        for p, u in remain:
            print(f"  仍有未使用: {p}: {u}")
        return 1
    print("  ✓ 无未使用导入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
