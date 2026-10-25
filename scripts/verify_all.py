"""一键验收：在隔离数据目录跑完整链路，不碰正式演示数据。

用法:
  python scripts/verify_all.py            # 全新隔离库跑一遍
  python scripts/verify_all.py --keep     # 跑完保留隔离库供检查

做这些：
  1. 建隔离数据目录并初始化
  2. 起独立端口服务
  3. pytest / seed / e2e / audit / demo / 数据源接入实测 / 截图
  4. 输出汇总，最后清理

注意事项：
  - 隔离成立与否，判据是"正式库会话数不变"
  - 服务必须重启后才跑，避免跑到旧代码（本项目无热加载）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISODIR = ROOT / "data_verify"
PORT = 8131
BASE = f"http://127.0.0.1:{PORT}"
PY = sys.executable


def wait_up(timeout: float = 45.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{BASE}/auth/login", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return False


def run(cmd: list[str], env: dict, label: str, tail: int = 2500) -> int:
    print(f"\n{'=' * 72}\n▶ {label}\n{'=' * 72}", flush=True)
    p = subprocess.run(cmd, cwd=str(ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    print(out[-tail:], flush=True)
    return p.returncode


def official_conv_count() -> int:
    """读正式库的会话数，用于对比隔离是否成立。"""
    code = (
        "import sys; sys.path.insert(0,'.');"
        "from app.db import init_engines, get_session, Conversation;"
        "init_engines();"
        "s=get_session();"
        "print(s.query(Conversation).count());"
        "s.close()"
    )
    p = subprocess.run([PY, "-c", code], cwd=str(ROOT),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        return int((p.stdout or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    before = official_conv_count()
    print(f"正式库当前会话数：{before}")

    if ISODIR.exists():
        shutil.rmtree(ISODIR, ignore_errors=True)
    ISODIR.mkdir(parents=True)

    env = dict(os.environ)
    env["ATTR_DATA_DIR"] = str(ISODIR)

    rc = run([PY, "scripts/init_db.py", "--force"], env, "① 初始化隔离数据库")
    if rc != 0:
        print("✗ 初始化失败")
        return rc

    print(f"\n{'=' * 72}\n▶ ② 启动隔离实例 {BASE}\n{'=' * 72}", flush=True)
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    results: dict[str, int] = {}
    try:
        if not wait_up():
            print("✗ 服务未在超时内就绪")
            return 1
        print(f"✓ 已就绪：{BASE}")

        # 造一个可登记的数据源文件
        run([PY, "scripts/make_demo_source.py"], env, "③ 生成演示数据源文件")

        results["pytest"] = run([PY, "-m", "pytest", "tests/", "-q"], env,
                                "④ 单元 + 接口测试")
        results["seed"] = run([PY, "scripts/seed_demo.py", BASE], env,
                              "⑤ 生成演示会话")
        results["e2e"] = run([PY, "scripts/e2e.py", BASE], env,
                             "⑥ 真实 HTTP 端到端验收")
        results["audit"] = run([PY, "scripts/audit_spec.py", BASE], env,
                               "⑦ 2.1 原文逐条审计")
        results["demo"] = run([PY, "scripts/demo.py", BASE], env,
                              "⑧ 两个业务场景链路")
        results["datasource"] = run([PY, "scripts/demo_datasource.py", BASE], env,
                                    "⑨ 数据源接入实测")

        shot_env = dict(env)
        shot_env["BASE"] = BASE
        results["shots"] = run(["node", "scripts/shoot.js"], shot_env, "⑩ 工作台截图")
        run(["node", "scripts/shoot_admin.js"], shot_env, "⑪ 管理员页截图")
        run(["node", "scripts/shoot_sources.js"], shot_env, "⑫ 数据源/用户页截图")

        # 隔离校验
        after = official_conv_count()
        print(f"\n{'=' * 72}\n▶ ⑬ 数据隔离校验\n{'=' * 72}")
        print(f"  正式库会话数：{before} → {after}")
        iso_ok = (before == after)
        if iso_ok:
            print("  ✓ 正式库未被写入，隔离成立")
        else:
            print("  ✗ 正式库发生变化，隔离被破坏")
            results["isolation"] = 1

        print(f"\n{'=' * 72}\n▶ 汇总\n{'=' * 72}")
        allok = iso_ok
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
        if not args.keep:
            shutil.rmtree(ISODIR, ignore_errors=True)
            print(f"\n已清理隔离目录 {ISODIR.name}/")
        else:
            print(f"\n已保留隔离目录 {ISODIR.name}/")


if __name__ == "__main__":
    raise SystemExit(main())
