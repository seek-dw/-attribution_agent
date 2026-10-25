"""模拟别人 clone 后的首次启动，验证仓库内容是否自足。

做法：把"应该被上传的文件"复制到一个全新目录，在那里按 README 步骤跑一遍。
不碰原项目，也不依赖任何 data/ 下的既有文件。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLONE = ROOT.parent / "_clone_test"
PORT = 8137
BASE = f"http://127.0.0.1:{PORT}"
PY = sys.executable

# 与 .gitignore 互补：只复制会被提交的内容
INCLUDE = ["app", "scripts", "tests", "shots",
           "README.md", "HANDOFF.md", "LICENSE", ".gitignore",
           "pyproject.toml", "requirements.txt"]


def main() -> int:
    if CLONE.exists():
        shutil.rmtree(CLONE, ignore_errors=True)
    CLONE.mkdir(parents=True)

    print(f"模拟目录：{CLONE}")
    for item in INCLUDE:
        src = ROOT / item
        if not src.exists():
            print(f"  ✗ 缺失：{item}")
            return 1
        dst = CLONE / item
        if src.is_dir():
            shutil.copytree(src, dst,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)
    print(f"✓ 已复制 {len(INCLUDE)} 项")

    # 关键检查：确认没有把敏感文件带过去
    leaks = []
    for pat in ("*.db", "secret.key", "runtime_config.json"):
        leaks += [str(p.relative_to(CLONE)) for p in CLONE.rglob(pat)]
    leaks = [x for x in leaks if "shots" not in x]
    if leaks:
        print(f"✗ 泄露敏感文件：{leaks}")
        return 1
    print("✓ 无数据库/密钥文件")

    # 用原 venv 的 python 跑（不重新装依赖，只验证代码与数据自足）
    env = dict(os.environ)
    env["ATTR_DATA_DIR"] = str(CLONE / "data")
    env["PYTHONPATH"] = str(CLONE)

    print("\n① 初始化（模拟 clone 后第一次运行）")
    p = subprocess.run([PY, "scripts/init_db.py", "--force"],
                       cwd=str(CLONE), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print((p.stdout or "") + (p.stderr or ""))
        print("✗ 初始化失败")
        return 1
    print("✓ 初始化成功")
    for line in (p.stdout or "").strip().splitlines()[-3:]:
        print(f"    {line}")

    print("\n② 启动服务")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT)],
        cwd=str(CLONE), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ok = False
        t0 = time.time()
        while time.time() - t0 < 40:
            try:
                with urllib.request.urlopen(f"{BASE}/auth/login", timeout=2) as r:
                    if r.status == 200:
                        ok = True
                        break
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
        if not ok:
            print("✗ 服务未就绪")
            return 1
        print(f"✓ {BASE}/auth/login 返回 200")

        print("\n③ 跑测试（验证代码完整）")
        p = subprocess.run([PY, "-m", "pytest", "tests/", "-q"],
                           cwd=str(CLONE), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        tail = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        print(f"    {tail}")
        if p.returncode != 0:
            print((p.stdout or "")[-1500:])
            print("✗ 测试未通过")
            return 1

        print("\n④ 跑原文审计")
        p = subprocess.run([PY, "scripts/audit_spec.py", BASE],
                           cwd=str(CLONE), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        out = p.stdout or ""
        summary = [l for l in out.splitlines() if "完全符合" in l]
        print(f"    {summary[0] if summary else '(无汇总)'}")
        # 打印非通过项，便于定位
        bad = [l for l in out.splitlines()
               if l.strip().startswith(("△", "✗"))]
        if bad:
            print("    非通过项：")
            for l in bad:
                print(f"      {l.strip()}")
        if p.returncode != 0:
            print("✗ 审计未全过")
            return 1

        print("\n" + "=" * 60)
        print("✓ clone 后可直接运行")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(CLONE, ignore_errors=True)
        print(f"\n已清理模拟目录")


if __name__ == "__main__":
    raise SystemExit(main())
