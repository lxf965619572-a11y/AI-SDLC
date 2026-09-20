"""把工具链的原始输出解析成可判定的结构化结论。

第一性原理：验证结论必须能被复核，所以解析器要满足两条——
  1. 只认工具真实打印的文本，不做任何「大概成功了」的推测；
  2. 解析不出来的东西显式标成缺失（missing / None），不静默当成通过。

三类输入：
  build.sh 的分段输出（WB_SECTION env/build/run/coverage/end）
  测试桩的用例结果行（TC-xxx PASS / TC-xxx FAIL 原因）
  gcov -b -c -f 的传统文本输出（兼容 7.5，不依赖 --json-format）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------- 分段输出 ----------------
_SECTION_RE = re.compile(r"^WB_SECTION\s+(\S+)\s*$")

KNOWN_SECTIONS = ("env", "build", "run", "coverage", "end")


def parse_sections(text: str) -> dict:
    """按 WB_SECTION 标记切分 build.sh 输出。缺失的段落给空串。

    为什么用显式标记而不是靠退出码：编译告警、用例失败、覆盖率文本会混在同一条
    stdout 里，只有分段才能把「工具链本身跑通了」和「被测代码有问题」区分开。"""
    out = {name: [] for name in KNOWN_SECTIONS}
    cur = None
    for line in (text or "").splitlines():
        m = _SECTION_RE.match(line.strip())
        if m:
            name = m.group(1)
            cur = name if name in out else None
            continue
        if cur:
            out[cur].append(line)
    return {k: "\n".join(v).strip("\n") for k, v in out.items()}


def parse_kv(text: str) -> dict:
    """解析 key=value 行（env 段）。"""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


# ---------------- 编译告警 ----------------
_GCC_DIAG = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+)(?::(?P<col>\d+))?:\s*"
                       r"(?P<level>warning|error|note):\s*(?P<msg>.*)$")


def parse_diagnostics(text: str) -> list:
    """从 gcc 输出里挑出 warning / error 行（note 是附注，单列会淹没结论）。"""
    out = []
    for line in (text or "").splitlines():
        m = _GCC_DIAG.match(line.strip())
        if not m or m.group("level") == "note":
            continue
        out.append({"file": m.group("file"), "line": int(m.group("line")),
                    "col": int(m.group("col") or 0),
                    "level": m.group("level"), "msg": m.group("msg").strip()})
    return out


# ---------------- 测试用例结果 ----------------
# 约定由 tests/wb_harness.c 打印，格式固定：`TC-001 PASS` / `TC-001 FAIL 原因`
_CASE_RE = re.compile(r"^\s*(?P<id>TC-[A-Za-z0-9_.\-]+)\s+(?P<st>PASS|FAIL)\b[ \t]*(?P<detail>.*)$")
_TOTAL_RE = re.compile(r"^WB_TOTAL\s+(\d+)\s*$")
_FAILED_RE = re.compile(r"^WB_FAILED\s+(\d+)\s*$")
_RUN_EXIT_RE = re.compile(r"^WB_RUN_EXIT\s+(-?\d+)\s*$")

ST_PASS = "pass"
ST_FAIL = "fail"
ST_MISSING = "missing"      # 用例在测试桩里存在，但没打出结果（进程中途崩了）

STATUS_TEXT = {ST_PASS: "通过", ST_FAIL: "失败", ST_MISSING: "未执行"}


@dataclass
class CaseResult:
    id: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "detail": self.detail,
                "status_text": STATUS_TEXT.get(self.status, self.status)}


def norm_case_id(value) -> str:
    """TC-1 / tc-001 / TC_001 → TC-001，与 core.trace 的编号口径保持一致。"""
    s = str(value or "").strip()
    m = re.match(r"^TC[\s_\-]*(\d+)$", s, re.IGNORECASE)
    if m:
        return f"TC-{int(m.group(1)):03d}"
    m = re.match(r"^TC[\s_\-]*([A-Za-z0-9].*)$", s, re.IGNORECASE)
    if m:
        return "TC-" + m.group(1).strip().replace("_", "-")
    return s


def parse_test_output(text: str, expected_ids=None) -> dict:
    """解析测试进程 stdout。

    expected_ids 给定时（来自 test_impl 元数据的用例清单），没打出结果的用例记为
    missing——段错误、断言宏写错导致进程提前退出时，「少了哪几条」正是定位线索。"""
    results: list[CaseResult] = []
    seen: dict[str, CaseResult] = {}
    total = failed = None
    run_exit = None
    for line in (text or "").splitlines():
        s = line.strip()
        m = _CASE_RE.match(s)
        if m:
            cid = norm_case_id(m.group("id"))
            res = CaseResult(cid, ST_PASS if m.group("st") == "PASS" else ST_FAIL,
                             m.group("detail").strip())
            if cid in seen:          # 同一编号打两次：以最后一次为准，但都留痕
                results = [r for r in results if r.id != cid]
            seen[cid] = res
            results.append(res)
            continue
        m = _TOTAL_RE.match(s)
        if m:
            total = int(m.group(1))
            continue
        m = _FAILED_RE.match(s)
        if m:
            failed = int(m.group(1))
            continue
        m = _RUN_EXIT_RE.match(s)
        if m:
            run_exit = int(m.group(1))

    expected = [norm_case_id(x) for x in (expected_ids or [])]
    for cid in expected:
        if cid not in seen:
            res = CaseResult(cid, ST_MISSING, "测试进程未输出该用例结果（可能提前退出）")
            seen[cid] = res
            results.append(res)
    results.sort(key=lambda r: r.id)

    n_pass = sum(1 for r in results if r.status == ST_PASS)
    n_fail = sum(1 for r in results if r.status == ST_FAIL)
    n_miss = sum(1 for r in results if r.status == ST_MISSING)
    crashed = run_exit is not None and run_exit not in (0, 1)
    return {
        "results": [r.to_dict() for r in results],
        "by_id": {r.id: r.to_dict() for r in results},
        "expected": expected,
        "total": len(results),
        "passed": n_pass,
        "failed": n_fail,
        "missing": n_miss,
        "reported_total": total,
        "reported_failed": failed,
        "run_exit": run_exit,
        "crashed": crashed,
        # 自报数与实际解析数对不上，说明测试桩自己有问题（漏调 wb_report / 提前 return），
        # 这类不一致必须暴露出来，否则「全绿」可能是假结论。
        "consistent": (total is None or total == len(results))
                      and (failed is None or failed == n_fail),
        "all_pass": bool(results) and n_fail == 0 and n_miss == 0 and not crashed,
    }


# ---------------- gcov 文本 ----------------
_FUNC_HEAD = re.compile(r"^Function\s+'(?P<name>.*)'\s*$")
_FILE_HEAD = re.compile(r"^File\s+'(?P<name>.*)'\s*$")
# 同时接受 `100.00% of 5`（默认）与 `3 of 5`（-c/--branch-counts）两种写法
_PCT_METRIC = re.compile(r"^(?P<key>Lines executed|Branches executed|Taken at least once|"
                         r"Calls executed):\s*(?P<val>[\d.]+%)\s*of\s*(?P<tot>\d+)\s*$")
_CNT_METRIC = re.compile(r"^(?P<key>Lines executed|Branches executed|Taken at least once|"
                         r"Calls executed):\s*(?P<hit>\d+)\s*of\s*(?P<tot>\d+)\s*$")
_NEVER = re.compile(r"^Never executed\s*$")
_NO_BRANCH = re.compile(r"^No branches\s*$")
_NO_CALLS = re.compile(r"^No calls\s*$")
# gcov 每处理完一个文件会打一行 `Creating 'x.c.gcov'`：这是它自己的产物提示，
# 不是覆盖率结论，混进 raw 会让证据文本多出一堆与被测代码无关的行。
_CREATING = re.compile(r"^Creating\s+'.*\.gcov'\s*$")

# 分支覆盖取「Taken at least once」：它回答的是「每个分支的两个方向是否都走到过」，
# 而 Branches executed 只说明分支被求值过，两者混用会高判覆盖。
_KEYS = {"lines": "Lines executed", "branches": "Taken at least once",
         "branches_eval": "Branches executed", "calls": "Calls executed"}


@dataclass
class CovBlock:
    """一段 gcov 结论（函数级或文件级）。"""
    kind: str                     # function | file
    name: str
    file: str = ""                # 函数块归属的源文件（由调用方按 gcda 对应关系补）
    lines_pct: float | None = None
    lines_total: int = 0
    lines_hit: int = 0
    branch_pct: float | None = None
    branch_total: int = 0
    branch_taken: int = 0
    branch_eval_pct: float | None = None
    calls_pct: float | None = None
    calls_total: int = 0
    no_branches: bool = False
    never_executed: bool = False
    raw: list = field(default_factory=list)

    @property
    def branch_effective(self) -> float:
        """用于门限判定的分支覆盖率：无分支的函数记 100%（不存在未覆盖的分支）。"""
        if self.branch_pct is not None:
            return self.branch_pct
        return 100.0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "file": self.file,
                "lines_pct": self.lines_pct, "lines_total": self.lines_total,
                "lines_hit": self.lines_hit,
                "branch_pct": self.branch_pct, "branch_total": self.branch_total,
                "branch_taken": self.branch_taken,
                "branch_effective": self.branch_effective,
                "calls_pct": self.calls_pct, "calls_total": self.calls_total,
                "no_branches": self.no_branches,
                "never_executed": self.never_executed}


def _apply_metric(blk: CovBlock, key: str, pct: float | None, hit: int | None,
                  total: int) -> None:
    if key == _KEYS["lines"]:
        blk.lines_total = total
        if pct is not None:
            blk.lines_pct = pct
            blk.lines_hit = int(round(total * pct / 100.0))
        else:
            blk.lines_hit = hit or 0
            blk.lines_pct = (100.0 * blk.lines_hit / total) if total else None
    elif key == _KEYS["branches"]:
        blk.branch_total = total
        if pct is not None:
            blk.branch_pct = pct
            blk.branch_taken = int(round(total * pct / 100.0))
        else:
            blk.branch_taken = hit or 0
            blk.branch_pct = (100.0 * blk.branch_taken / total) if total else None
    elif key == _KEYS["branches_eval"]:
        blk.branch_eval_pct = pct
    elif key == _KEYS["calls"]:
        blk.calls_total = total
        blk.calls_pct = pct


def parse_gcov(text: str, default_file: str = "") -> dict:
    """解析 `gcov -b -c -f` 的文本输出。

    返回 {"functions": {名: CovBlock}, "files": {路径: CovBlock}, "order": [...]}。
    default_file 用来给函数块补归属：gcov 的 Function 块不带文件名，而我们是一次
    gcda 调一次 gcov，调用方知道这次对应哪个源文件。"""
    functions: dict[str, CovBlock] = {}
    files: dict[str, CovBlock] = {}
    order: list[str] = []
    cur: CovBlock | None = None

    def flush():
        nonlocal cur
        if cur is None:
            return
        if cur.kind == "function":
            if cur.name in functions:      # 同名 static 函数出现在多个文件：按文件区分
                functions[f"{cur.file or default_file}::{cur.name}"] = cur
            else:
                functions[cur.name] = cur
        else:
            files[cur.name] = cur
        cur = None

    for line in (text or "").splitlines():
        s = line.rstrip()
        st = s.strip()
        m = _FUNC_HEAD.match(st)
        if m:
            flush()
            cur = CovBlock("function", m.group("name"), file=default_file)
            order.append(f"function:{m.group('name')}")
            continue
        m = _FILE_HEAD.match(st)
        if m:
            flush()
            cur = CovBlock("file", m.group("name"))
            order.append(f"file:{m.group('name')}")
            continue
        if cur is None:
            continue
        if _CREATING.match(st):
            continue
        cur.raw.append(st)
        m = _PCT_METRIC.match(st)
        if m:
            _apply_metric(cur, m.group("key"),
                          float(m.group("val").rstrip("%")), None,
                          int(m.group("tot")))
            continue
        m = _CNT_METRIC.match(st)
        if m:
            _apply_metric(cur, m.group("key"), None,
                          int(m.group("hit")), int(m.group("tot")))
            continue
        if _NEVER.match(st):
            cur.never_executed = True
            if cur.lines_pct is None:
                cur.lines_pct = 0.0
        elif _NO_BRANCH.match(st):
            cur.no_branches = True
        elif _NO_CALLS.match(st):
            pass
    flush()
    return {"functions": functions, "files": files, "order": order}


def is_project_file(path: str, roots=("src/", "include/", "tests/")) -> bool:
    """gcov 会把系统头也列出来，覆盖率结论只认本项目文件。"""
    p = str(path or "").replace("\\", "/")
    if not p or p.startswith("/") or ":" in p[:3]:
        return False
    return any(p.startswith(r) or f"/{r}" in p for r in roots)


_GCOV_TARGET = re.compile(r"^WB_GCOV_TARGET\s+(?P<path>\S.*)$")


def parse_gcov_multi(text: str) -> dict:
    """解析 build.sh coverage 段：多次 gcov 调用按 WB_GCOV_TARGET 标记归属源文件。

    每个编译单元只保留「与本轮目标同名」的 File 块：头文件被多个 .c 包含时，
    gcov 会按各自的视角重复报告它，直接合并会重复计数。"""
    merged: dict[str, CovBlock] = {}
    files: dict[str, CovBlock] = {}
    order: list[str] = []
    target = ""
    chunk: list[str] = []

    def flush():
        nonlocal chunk
        if not chunk:
            return
        parsed = parse_gcov("\n".join(chunk), default_file=target)
        for name, blk in parsed["functions"].items():
            key = name if name not in merged else f"{blk.file or target}::{name}"
            merged[key] = blk
            order.append(f"function:{key}")
        for name, blk in parsed["files"].items():
            norm = str(name).replace("\\", "/")
            if target and norm != target and not norm.endswith("/" + target):
                continue        # 头文件与系统文件不计入本编译单元的结论
            files[target or norm] = blk
        chunk = []

    for line in (text or "").splitlines():
        m = _GCOV_TARGET.match(line.strip())
        if m:
            flush()
            target = m.group("path").strip().replace("\\", "/")
            continue
        chunk.append(line)
    flush()
    if not files and not merged:
        # 没有标记时退化为单块解析（手工调试、旧日志仍然可用）
        parsed = parse_gcov(text or "")
        merged, files, order = parsed["functions"], parsed["files"], parsed["order"]
    return {"functions": merged, "files": files, "order": order}


def summarize_coverage(parsed: dict, branch_min: float = 0.0,
                       line_min: float = 0.0) -> dict:
    """把 gcov 解析结果汇成可判定的覆盖率结论。

    口径：
      函数级 branch_effective 取该函数「Taken at least once」，无分支记 100%；
      文件级只统计 src/ 下的实现文件，测试桩自身的覆盖率不计入交付判据；
      总体分支覆盖用「已覆盖分支数 / 分支总数」加权，不是各文件百分比的平均——
      平均会让一个 3 行的小文件和一个 300 行的核心模块权重相同。"""
    funcs = []
    for name, blk in (parsed.get("functions") or {}).items():
        d = blk.to_dict()
        d["key"] = name
        # 判据只落在被测代码上：测试桩里的辅助函数覆盖不全不是交付缺陷
        # （用例是否真跑过由 parse_test_output 的 missing 判定）。
        # 归属未知（手工日志没有 WB_GCOV_TARGET 标记）时保守地留在判据内，
        # 否则「解析不出归属」会被静默当成「不需要判」。
        d["in_scope"] = (not blk.file) or is_project_file(blk.file, ("src/", "include/"))
        funcs.append(d)
    funcs.sort(key=lambda d: d["key"])
    gated = [d for d in funcs if d["in_scope"]]

    src_files = []
    for name, blk in (parsed.get("files") or {}).items():
        if not is_project_file(name, ("src/",)):
            continue
        d = blk.to_dict()
        src_files.append(d)
    src_files.sort(key=lambda d: d["name"])

    tot_lines = sum(d["lines_total"] for d in src_files)
    hit_lines = sum(d["lines_hit"] for d in src_files)
    tot_br = sum(d["branch_total"] for d in src_files)
    taken_br = sum(d["branch_taken"] for d in src_files)
    line_pct = (100.0 * hit_lines / tot_lines) if tot_lines else None
    branch_pct = (100.0 * taken_br / tot_br) if tot_br else None

    # 函数级门限：只对真正有分支的函数判分支覆盖，无分支的函数按行覆盖判
    below_branch = [d["key"] for d in gated
                    if d["branch_total"] and d["branch_effective"] < branch_min]
    below_line = [d["key"] for d in gated
                  if d["lines_total"] and (d["lines_pct"] or 0.0) < line_min]
    # 「一次都没被执行」是独立于百分比门限的硬缺陷：总体覆盖率再高，
    # 也掩盖不了某个函数背后的需求根本没被验证过。lines_total==0 的块
    # 是没有可执行语句的声明式条目，不算漏测，避免误判。
    untested = [d["key"] for d in gated
                if d["never_executed"]
                or (d["lines_total"] and (d["lines_pct"] or 0.0) <= 0.0)]

    return {
        "functions": funcs,
        "function_map": {d["name"]: d for d in funcs},
        "files": src_files,
        "totals": {"lines_total": tot_lines, "lines_hit": hit_lines,
                   "line_pct": line_pct,
                   "branch_total": tot_br, "branch_taken": taken_br,
                   "branch_pct": branch_pct},
        "thresholds": {"branch_min": branch_min, "line_min": line_min},
        "below_branch": sorted(below_branch),
        "below_line": sorted(below_line),
        "untested_functions": sorted(untested),
        "ok": (not below_branch and not below_line and not untested
               and (branch_pct is None or branch_pct >= branch_min)
               and (line_pct is None or line_pct >= line_min)),
    }
