"""一键跑完 tests/ 下所有零依赖测试脚本。

每个 test_*.py 都是自带 _main() 的独立脚本（不用 pytest，装完依赖即可跑）。
这里用子进程逐个执行，好处是各文件的全局状态、monkeypatch 互不污染，
某个文件崩溃也不会带垮其余文件；退出码非 0 即视为该文件失败。

用法：.venv\\Scripts\\python.exe tests\\run_all.py [-v]
"""
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def main() -> int:
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    files = sorted(p for p in TESTS_DIR.glob("test_*.py"))
    if not files:
        print("没有找到 test_*.py")
        return 1

    failed = []
    for path in files:
        proc = subprocess.run([sys.executable, str(path)],
                              cwd=str(TESTS_DIR.parent),
                              capture_output=not verbose, text=True,
                              encoding="utf-8", errors="replace")
        if verbose:
            ok = proc.returncode == 0
        else:
            out = (proc.stdout or "") + (proc.stderr or "")
            # 脚本末尾统一打印 "N/M passed"，取来做单行摘要
            summary = next((ln.strip() for ln in reversed(out.splitlines())
                            if "passed" in ln), "")
            ok = proc.returncode == 0
            print(("PASS  " if ok else "FAIL  ") + path.name +
                  ("  " + summary if summary else ""))
            if not ok:
                print(out.rstrip())
        if not ok:
            failed.append(path.name)

    print("\n%d/%d 个测试文件通过" % (len(files) - len(failed), len(files)))
    if failed:
        print("失败：" + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
