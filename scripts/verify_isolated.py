"""在隔离数据目录里跑完整验收 + 截图，不碰正式演示数据。

用法:
  python scripts/verify_isolated.py

做三件事：
  1. 用 ATTR_DATA_DIR=data_verify 启动一个独立后端
  2. 在该实例上跑 e2e / audit_spec / demo / 截图
  3. 关掉后清理该目录

这样反复验收也不会在正式库里堆积重复会话。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERIFY_DIR = ROOT / "data_verify"
PORT = 8131
BASE = f"http://127.0.0.1:{PORT}"
PY = sys.executable


def wait_up(timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{BASE}/auth/login", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return False


def run(cmd: list[str], env: dict | None = None, label: str = "") -> int:
    print(f"\n{'=' * 70}\n▶ {label}\n{'=' * 70}")
    p = subprocess.run(cmd, cwd=str(ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    print(out[-3000:])
    return p.returncode


def main() -> int:
    if VERIFY_DIR.exists():
        shutil.rmtree(VERIFY_DIR, ignore_errors=True)
    VERIFY_DIR.mkdir(parents=True)

    env = dict(os.environ)
    env["ATTR_DATA_DIR"] = str(VERIFY_DIR)

    # 1. 初始化隔离库
    rc = run([PY, "scripts/init_db.py", "--force"], env, "初始化隔离数据库")
    if rc != 0:
        print("✗ 初始化失败")
        return rc

    # 2. 起服务
    print(f"\n{'=' * 70}\n▶ 启动隔离实例 {BASE}（数据在 {VERIFY_DIR.name}/）\n{'=' * 70}")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_up():
            print("✗ 服务未在超时内就绪")
            return 1
        print(f"✓ 已就绪：{BASE}")

        results: dict[str, int] = {}

        # 3. 种演示数据
        results["seed_demo"] = run([PY, "scripts/seed_demo.py", BASE], env,
                                   "生成演示会话")

        # 4. 单测（用隔离库）
        results["pytest"] = run([PY, "-m", "pytest", "tests/", "-q"], env,
                                "单元 + 接口测试")

        # 5. 端到端
        results["e2e"] = run([PY, "scripts/e2e.py", BASE], env,
                             "真实 HTTP 端到端验收")

        # 6. 原文审计
        results["audit"] = run([PY, "scripts/audit_spec.py", BASE], env,
                               "2.1 原文逐条审计")

        # 7. 两场景演示
        results["demo"] = run([PY, "scripts/demo.py", BASE], env,
                              "两个业务场景链路")

        # 8. 截图（隔离库，随便造）
        shot_env = dict(env)
        shot_env["BASE"] = BASE
        results["shots"] = run(["node", "scripts/shoot.js"], shot_env, "工作台截图")
        run(["node", "scripts/shoot_admin.js"], shot_env, "管理员页截图")

        print(f"\n{'=' * 70}\n▶ 汇总\n{'=' * 70}")
        allok = True
        for k, v in results.items():
            mark = "✓" if v == 0 else "✗"
            if v != 0:
                allok = False
            print(f"  {mark} {k:<12} exit={v}")
        return 0 if allok else 1

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"\n隔离实例已停止。数据目录 {VERIFY_DIR.name}/ 保留供检查，可手动删除。")


if __name__ == "__main__":
    raise SystemExit(main())
