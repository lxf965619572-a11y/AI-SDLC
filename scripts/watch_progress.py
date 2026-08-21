"""流水线进度实时监视：每 15 秒刷新一次，Ctrl+C 退出。
用法：python scripts/watch_progress.py [--once]（--once 只刷新一屏就退出）"""
import json
import os
import sys
import time

import httpx

BASE = "http://127.0.0.1:5100"
STAGE_NAMES = {
    "parse": "①输入解析", "requirement": "②需求分析", "hld": "③概要设计",
    "lld": "④详细设计", "testcase": "⑤测试用例",
}
STATUS_CN = {
    "created": "待启动", "parsing": "解析中", "running": "执行中",
    "waiting_review": "⏸ 等待评审", "completed": "✔ 已完成", "failed": "✘ 失败",
}


def main():
    once = "--once" in sys.argv
    while True:
        if os.name == "nt" and not once:
            os.system("cls")
        ts = time.strftime("%H:%M:%S")
        print("=" * 62)
        print(f"  流水线进度监视  更新于 {ts}")
        print("=" * 62)
        try:
            projects = httpx.get(f"{BASE}/api/projects", timeout=5).json()
            for p in projects:
                st = httpx.get(f"{BASE}/api/projects/{p['id']}/status", timeout=5).json()
                stage_cn = STAGE_NAMES.get(st.get("current_stage") or "", "-")
                print(f"\n[{p['id']}] {p['name']}")
                print(f"    状态: {STATUS_CN.get(st['status'], st['status'])}   当前阶段: {stage_cn}")
                arts = st.get("artifacts") or {}
                if arts:
                    print("    已产出: " + ", ".join(
                        f"{STAGE_NAMES.get(k, k)}(v{v['version']},{v['status']})"
                        for k, v in arts.items()))
                logs = httpx.get(f"{BASE}/api/projects/{p['id']}/logs", timeout=5).json()
                recent = [l for l in logs][-4:]
                for l in recent:
                    print(f"    [{l['time']}] {l['message'][:110]}")
        except Exception as e:
            print(f"  查询失败: {e}")
        if once:
            break
        print("\n  （每 15 秒刷新，Ctrl+C 退出）")
        time.sleep(15)


if __name__ == "__main__":
    main()
