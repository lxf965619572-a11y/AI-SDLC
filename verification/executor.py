"""验证执行编排：同步代码 → 远端构建运行 → 解析结论 → 归档证据。

第一性原理：这一层产出的不是「一段日志」，而是一个可判定的结论 + 支撑该结论的
完整证据。所以每次执行都固定带回四样东西：
  1. 结论（build / tests / coverage 三个维度各自的判定，以及总判定）；
  2. 输入指纹（同步前在本机算好的 sha256，证明跑的就是这份代码）；
  3. 环境指纹（uname、gcc、gcov 版本）；
  4. 原始输出（命令行、退出码、stdout/stderr 全文）。
缺任何一样，结论都不能当交付证据用。

本模块不碰数据库、不碰 LLM：节点层负责落库与路由，这里只负责「跑出结论」。
"""
from __future__ import annotations

import json
import shlex
import time
from pathlib import Path

from verification import buildkit, parsers

# 原始日志入库上限：SQLite 里存全文便于追溯，但要防住异常输出把库撑爆
RAW_LOG_LIMIT = 200_000


def _shq(path: str) -> str:
    """POSIX 单引号转义。远端命令里的路径一律经它包裹。"""
    return shlex.quote(str(path))

VERDICT_OK = "ok"
VERDICT_FAIL = "fail"
VERDICT_SKIPPED = "skipped"

# 路由决策：exec 节点据此决定去 report 还是回哪个阶段重生
DECISION_NEXT = "next"            # 全绿，进交付件装配
DECISION_FIX_CODE = "fix_code"    # 判为代码缺陷，回 code 阶段
DECISION_FIX_TEST = "fix_test"    # 判为测试缺陷，回 test_impl 阶段
DECISION_BLOCKED = "blocked"      # 执行环境不可用，转人工


def _clip(text: str, limit: int = RAW_LOG_LIMIT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，全文 {len(text)} 字符，完整内容见证据归档]"


def archive_evidence(outcome: dict, project_id: int, version, stage: str = "exec") -> str:
    """把原始日志与结论落到证据目录，返回相对路径。

    为什么既要入库又要落盘：入库的那份会被截断，落盘的是完整原文；
    出争议时要能拿出未截断的原始输出。"""
    import config

    base = Path(config.VERIFY_EVIDENCE_DIR) / f"p{project_id}" / f"v{version}"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{stage}.log"
    try:
        path.write_text(outcome.get("raw_log") or "", encoding="utf-8")
        meta = base / f"{stage}.json"
        slim = {k: v for k, v in outcome.items() if k != "raw_log"}
        meta.write_text(json.dumps(slim, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    except OSError:
        return ""
    return str(path).replace("\\", "/")


# ---------------- 编译探针（代码 / 测试实现阶段的硬校验） ----------------
def compile_probe(runner, project_id: int, code_files: dict,
                  test_files: dict | None = None, stage: str = "code",
                  timeout: int | None = None) -> dict:
    """远端编译校验。返回 {"ok", "reason", "log", "exit_code", "skipped"}。

    test_files 为 None 时只编 src（代码阶段）；给了就连测试一起编并链接
    （测试实现阶段：链接通过才说明测试真的能跑起来）。
    runner 为 None 表示未配置验证机，此时不做远端编译，明确标 skipped——
    离线/mock 模式要能继续走，但绝不能把 skipped 当成 ok。"""
    link = test_files is not None
    if runner is None:
        return {"ok": True, "skipped": True, "exit_code": None, "log": "",
                "reason": "未配置验证机，跳过远端编译校验（WARN）"}

    files = buildkit.workspace_files(code_files, test_files)
    files.pop("build.sh", None)                 # 探针不运行、不采覆盖率
    files["check.sh"] = buildkit.check_script(link=link)

    workdir = f"{runner.workdir(project_id, 'probe')}/{stage}"
    # 探针目录每轮清空重来：上一轮的 .o 残留会让「其实编不过」的结论被掩盖
    runner.run(f"rm -rf {_shq(workdir)}", timeout=30)
    try:
        runner.sync(files, workdir)
        res = runner.run("sh check.sh", cwd=workdir, timeout=timeout)
    except Exception as e:                       # 连不上、超时、tar 缺失
        return {"ok": False, "skipped": False, "exit_code": None, "log": "",
                "reason": f"验证机不可用：{e}"}

    log = res.output
    ok = res.ok and "WB_BUILD_RESULT ok" in (res.stdout or "")
    if not ok and not log.strip():
        log = f"退出码 {res.exit_code}，无输出"
    return {"ok": ok, "skipped": False, "exit_code": res.exit_code,
            "log": _clip(log, 60_000), "reason": "" if ok else "远端编译未通过",
            "workdir": workdir, "cmd": res.cmd}


# ---------------- 完整验证运行 ----------------
def run_verification(runner, project_id: int, version,
                     code_files: dict, test_files: dict,
                     case_ids=None, branch_min: float = 80.0,
                     line_min: float = 0.0, timeout: int | None = None,
                     decision_cb=None) -> dict:
    """跑一轮完整验证：同步 → 构建运行 → 覆盖率 → 解析 → 出结论。"""
    t0 = time.time()
    base = {
        "project_id": project_id, "version": str(version),
        "duration_s": 0.0, "raw_log": "", "commands": [],
        "manifest": {}, "env": {}, "thresholds": {"branch_min": branch_min,
                                                   "line_min": line_min},
    }

    if runner is None:
        return {**base, "ok": False, "skipped": True, "decision": DECISION_BLOCKED,
                "reason": "未配置验证机（VERIFY_HOST 为空），无法执行编译与测试",
                "verdict": {"build": VERDICT_SKIPPED, "tests": VERDICT_SKIPPED,
                            "coverage": VERDICT_SKIPPED}}

    try:
        probe = runner.probe()
    except Exception as e:
        return {**base, "ok": False, "skipped": True, "decision": DECISION_BLOCKED,
                "reason": f"验证机探测失败：{e}",
                "verdict": {"build": VERDICT_SKIPPED, "tests": VERDICT_SKIPPED,
                            "coverage": VERDICT_SKIPPED}}
    if not probe.get("ok"):
        return {**base, "ok": False, "skipped": True, "decision": DECISION_BLOCKED,
                "reason": f"验证机工具链不可用：{probe.get('error') or '未知原因'}",
                "env": probe,
                "verdict": {"build": VERDICT_SKIPPED, "tests": VERDICT_SKIPPED,
                            "coverage": VERDICT_SKIPPED}}

    files = buildkit.workspace_files(code_files, test_files)
    workdir = runner.workdir(project_id, version)
    try:
        manifest = runner.sync(files, workdir)
    except Exception as e:
        return {**base, "ok": False, "skipped": True, "decision": DECISION_BLOCKED,
                "reason": f"同步到验证机失败：{e}", "env": probe, "workdir": workdir,
                "verdict": {"build": VERDICT_SKIPPED, "tests": VERDICT_SKIPPED,
                            "coverage": VERDICT_SKIPPED}}

    res = runner.run("sh build.sh", cwd=workdir, timeout=timeout)
    raw = res.output
    sections = parsers.parse_sections(res.stdout or "")
    env = {**{k: probe.get(k) for k in ("uname", "cores", "gcc", "gcov", "make", "tar")
              if probe.get(k)}, **parsers.parse_kv(sections.get("env", ""))}

    build_log = sections.get("build", "")
    diags = parsers.parse_diagnostics(build_log)
    build_ok = ("WB_BUILD_RESULT ok" in build_log) and res.exit_code != buildkit.EXIT_BUILD_FAIL
    no_source = res.exit_code == buildkit.EXIT_NO_SOURCE

    tests = parsers.parse_test_output(sections.get("run", ""), expected_ids=case_ids)
    cov_parsed = parsers.parse_gcov_multi(sections.get("coverage", ""))
    coverage = parsers.summarize_coverage(cov_parsed, branch_min=branch_min,
                                          line_min=line_min)

    verdict = {
        "build": VERDICT_OK if build_ok else VERDICT_FAIL,
        "tests": (VERDICT_OK if tests["all_pass"] else VERDICT_FAIL) if build_ok
                 else VERDICT_SKIPPED,
        "coverage": (VERDICT_OK if coverage["ok"] else VERDICT_FAIL) if build_ok
                    else VERDICT_SKIPPED,
    }
    ok = build_ok and tests["all_pass"] and coverage["ok"]

    decision = DECISION_NEXT if ok else DECISION_FIX_CODE
    reason = ""
    if not build_ok:
        reason = "无源文件可编译" if no_source else "编译或链接失败"
    elif not tests["all_pass"]:
        reason = f"{tests['failed']} 条用例失败" + (
            f"，{tests['missing']} 条未执行" if tests["missing"] else "")
    elif not coverage["ok"]:
        reason = _coverage_reason(coverage, branch_min, line_min)
    if not ok and decision_cb is not None:
        try:
            decision, reason = decision_cb(outcome_reason=reason, tests=tests,
                                           coverage=coverage, build_ok=build_ok,
                                           diags=diags) or (decision, reason)
        except Exception:
            pass

    outcome = {
        **base,
        "ok": ok, "skipped": False, "decision": decision, "reason": reason,
        "verdict": verdict, "env": env, "manifest": manifest,
        "workdir": workdir, "exit_code": res.exit_code,
        "duration_s": round(time.time() - t0, 3),
        "transport": res.transport,
        "commands": [{"cmd": res.cmd, "cwd": res.cwd, "exit_code": res.exit_code,
                      "duration_s": round(res.duration_s, 3)}],
        "synced_files": sorted(manifest.keys()),
        "build": {"ok": build_ok, "log": _clip(build_log, 60_000),
                  "diagnostics": diags,
                  "warnings": [d for d in diags if d["level"] == "warning"],
                  "errors": [d for d in diags if d["level"] == "error"]},
        "tests": tests,
        "coverage": coverage,
        "raw_log": _clip(raw),
        "run_section": _clip(sections.get("run", ""), 60_000),
        "coverage_section": _clip(sections.get("coverage", ""), 60_000),
    }
    return outcome


def _pct(value) -> str:
    return "-" if value is None else f"{value:.2f}%"


def _coverage_reason(coverage: dict, branch_min: float, line_min: float) -> str:
    """把覆盖率不达标说准确。

    「总体不够」和「某个函数不够」是两类问题：前者要么补测试要么改设计，
    后者往往是防御性分支没人走。归因智能体靠这句话决定回哪个阶段重生，
    口径含糊就会把测试缺陷判成代码缺陷。"""
    tot = coverage.get("totals") or {}
    fmap = coverage.get("function_map") or {}
    parts = []
    br = tot.get("branch_pct")
    if br is not None and br < branch_min:
        parts.append(f"总体分支覆盖 {_pct(br)} < {branch_min:g}%")
    ln = tot.get("line_pct")
    if line_min and ln is not None and ln < line_min:
        parts.append(f"总体行覆盖 {_pct(ln)} < {line_min:g}%")
    never = coverage.get("untested_functions") or []
    if never:
        parts.append("未被执行的函数：" + "、".join(never))
    below = coverage.get("below_branch") or []
    if below:
        detail = "、".join(
            f"{n} 分支 {_pct((fmap.get(n) or {}).get('branch_effective'))}"
            f" < {branch_min:g}%" for n in below)
        parts.append("函数分支覆盖不足：" + detail)
    below_line = coverage.get("below_line") or []
    if below_line:
        parts.append("函数行覆盖不足：" + "、".join(below_line))
    return "覆盖率未达门限（" + "；".join(parts) + "）" if parts else "覆盖率未达门限"


# ---------------- 失败归因输入整理 ----------------
def failure_brief(outcome: dict, limit: int = 20) -> str:
    """把一轮失败的验证结论压成归因智能体能读的简报。

    归因要判「代码缺陷还是测试缺陷」，需要的正是三样：编译器怎么说、
    哪条用例失败以及失败时打印了什么、哪些函数根本没被执行到。"""
    lines = []
    verdict = outcome.get("verdict") or {}
    lines.append(f"判定：构建 {verdict.get('build')} · 用例 {verdict.get('tests')} · "
                 f"覆盖率 {verdict.get('coverage')}")
    if outcome.get("reason"):
        lines.append(f"结论：{outcome['reason']}")

    build = outcome.get("build") or {}
    for d in (build.get("diagnostics") or [])[:limit]:
        lines.append(f"[{d['level']}] {d['file']}:{d['line']}: {d['msg']}")
    if not build.get("ok"):
        tail = (build.get("log") or "").strip().splitlines()[-15:]
        lines += [f"构建日志尾部：{ln}" for ln in tail]

    tests = outcome.get("tests") or {}
    for r in (tests.get("results") or []):
        if r["status"] != parsers.ST_PASS:
            lines.append(f"[{r['status'].upper()}] {r['id']} {r.get('detail','')}")
    if tests.get("crashed"):
        lines.append(f"测试进程异常退出，exit={tests.get('run_exit')}")
    if tests.get("consistent") is False:
        lines.append("测试桩自报的用例数与实际输出不一致，测试代码本身可能有问题")

    cov = outcome.get("coverage") or {}
    if cov.get("untested_functions"):
        lines.append("未被执行到的函数：" + "、".join(cov["untested_functions"]))
    if cov.get("below_branch"):
        lines.append("分支覆盖不足的函数：" + "、".join(cov["below_branch"]))
    totals = cov.get("totals") or {}
    if totals:
        lines.append(f"总体：行 {_pct(totals.get('line_pct'))}"
                     f"（{totals.get('lines_hit')}/{totals.get('lines_total')}）　"
                     f"分支 {_pct(totals.get('branch_pct'))}"
                     f"（{totals.get('branch_taken')}/{totals.get('branch_total')}）")
    return "\n".join(lines)


def _diag_side(path) -> str:
    """诊断落在被测代码还是测试代码。归因的第一手证据就是落点。"""
    p = str(path or "").replace("\\", "/")
    if p.startswith("tests/"):
        return "test"
    if p.startswith(("src/", "include/")):
        return "code"
    return ""


def auto_decision(outcome: dict) -> tuple[str, str] | None:
    """确定性归因：工具链已经给出确定答案的，不去问模型。

    归因是闭环的方向盘。方向判错，代码智能体就会去改一段本来正确的实现，
    越改越坏，还会把评审门的注意力引到无关处。所以凡是有确定判据的情形都在
    这里判死，只把真正需要读代码与用例做权衡的情形留给归因智能体：
      构建失败      诊断落点在 src/ 还是 tests/，落点即责任方；
      覆盖率不达标  用例全过而覆盖不足，缺的是用例，只能是测试侧；
      测试桩不自洽  自报条数与解析条数对不上，测试代码上报逻辑有误。
    返回 None 表示需要归因智能体介入（典型是用例判据与实现行为对不上，
    以及进程崩溃——两者都可能是任意一侧的问题）。
    """
    if outcome.get("skipped"):
        return DECISION_BLOCKED, outcome.get("reason") or "验证机不可用"

    build = outcome.get("build") or {}
    if not build.get("ok"):
        log_text = build.get("log") or ""
        diags = build.get("errors") or build.get("diagnostics") or []
        sides = [(d, _diag_side(d.get("file"))) for d in diags]
        code_hits = [d for d, s in sides if s == "code"]
        test_hits = [d for d, s in sides if s == "test"]
        if code_hits:
            d = code_hits[0]
            return DECISION_FIX_CODE, (f"构建失败，被测代码有诊断："
                                       f"{d.get('file')}:{d.get('line')} {d.get('msg')}")
        if test_hits:
            d = test_hits[0]
            return DECISION_FIX_TEST, (f"构建失败，诊断全部落在测试代码："
                                       f"{d.get('file')}:{d.get('line')} {d.get('msg')}")
        if "undefined reference" in log_text or "multiple definition" in log_text:
            return DECISION_FIX_TEST, f"链接失败（符号未定义或重复定义）：{_tail(log_text)}"
        return DECISION_FIX_CODE, f"构建失败：{_tail(log_text)}"

    tests = outcome.get("tests") or {}
    if tests.get("consistent") is False:
        return DECISION_FIX_TEST, (
            f"测试桩自报 total={tests.get('reported_total')} "
            f"failed={tests.get('reported_failed')}，与实际解析出的 "
            f"{tests.get('total')} 条不一致，测试代码的结果上报有误")

    cov = outcome.get("coverage") or {}
    if tests.get("all_pass") and not cov.get("ok"):
        parts = []
        if cov.get("untested_functions"):
            parts.append("未被执行的函数：" + "、".join(cov["untested_functions"]))
        if cov.get("below_branch"):
            parts.append("分支覆盖不足：" + "、".join(cov["below_branch"]))
        tot = cov.get("totals") or {}
        if tot.get("branch_pct") is not None:
            parts.append(f"总体分支覆盖 {_pct(tot.get('branch_pct'))}")
        return DECISION_FIX_TEST, ("用例全部通过而覆盖率不达标，缺的是用例不是实现："
                                   + "；".join(parts))
    return None


def _tail(text: str, n: int = 3) -> str:
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return " / ".join(lines[-n:]) or "（无输出）"


# ---------------- Markdown 报告 ----------------
def markdown_report(outcome: dict, title: str = "代码验证执行报告") -> str:
    """把一轮验证结论渲染成 Markdown（exec 阶段产物，前端直接展示）。"""
    if outcome.get("skipped"):
        return (f"# {title}\n\n> **未执行**：{outcome.get('reason') or '验证机不可用'}\n\n"
                "配置 .env 的 VERIFY_HOST / VERIFY_USER 后重跑本阶段即可采集真实结论。")

    verdict = outcome.get("verdict") or {}
    mark = {VERDICT_OK: "通过", VERDICT_FAIL: "不通过", VERDICT_SKIPPED: "未执行"}
    lines = [f"# {title}", "",
             f"> 总判定：**{'全部通过' if outcome.get('ok') else '未通过'}**　"
             f"构建 {mark.get(verdict.get('build'), '-')} · "
             f"用例 {mark.get(verdict.get('tests'), '-')} · "
             f"覆盖率 {mark.get(verdict.get('coverage'), '-')}"]
    if outcome.get("reason"):
        lines.append(f"> 原因：{outcome['reason']}")
    lines.append("")

    env = outcome.get("env") or {}
    lines += ["## 执行环境", "",
              "| 项 | 值 |", "|---|---|",
              f"| 传输方式 | {outcome.get('transport', '-')} |",
              f"| 目标机 | {env.get('uname', '-')} |",
              f"| 编译器 | {env.get('gcc', '-')} |",
              f"| 覆盖率工具 | {env.get('gcov', '-')} |",
              f"| 远端工作目录 | {outcome.get('workdir', '-')} |",
              f"| 编译选项 | {buildkit.CFLAGS} |",
              f"| 耗时 | {outcome.get('duration_s', 0)} s |", ""]

    tests = outcome.get("tests") or {}
    results = tests.get("results") or []
    lines += ["## 用例执行结果", "",
              f"> 共 {tests.get('total', 0)} 条：通过 {tests.get('passed', 0)} · "
              f"失败 {tests.get('failed', 0)} · 未执行 {tests.get('missing', 0)}", ""]
    if results:
        lines += ["| 用例 | 结果 | 说明 |", "|---|---|---|"]
        for r in results:
            detail = str(r.get("detail") or "").replace("|", "/").replace("\n", " ")
            lines.append(f"| {r['id']} | {r.get('status_text', r['status'])} | {detail} |")
        lines.append("")
    if tests.get("consistent") is False:
        lines += [f"> ⚠️ 测试桩自报 total={tests.get('reported_total')} "
                  f"failed={tests.get('reported_failed')}，与实际解析出的条数不一致。", ""]

    cov = outcome.get("coverage") or {}
    totals = cov.get("totals") or {}
    th = cov.get("thresholds") or {}
    lines += ["## 覆盖率", "",
              f"> 总体行覆盖 **{_pct(totals.get('line_pct'))}**"
              f"（{totals.get('lines_hit', 0)}/{totals.get('lines_total', 0)}）　"
              f"总体分支覆盖 **{_pct(totals.get('branch_pct'))}**"
              f"（{totals.get('branch_taken', 0)}/{totals.get('branch_total', 0)}）　"
              f"门限：分支 {th.get('branch_min', 0)}%", ""]
    funcs = cov.get("functions") or []
    if funcs:
        lines += ["| 函数 | 文件 | 行覆盖 | 分支覆盖 | 分支数 | 状态 |",
                  "|---|---|---|---|---|---|"]
        for d in funcs:
            bad = (d["branch_total"] and d["branch_effective"] < (th.get("branch_min") or 0))
            dead = d["never_executed"] or (d["lines_pct"] or 0) <= 0
            state = "未执行" if dead else ("分支不足" if bad else "达标")
            lines.append(f"| {d['name']} | {d.get('file') or '-'} | "
                         f"{_pct(d.get('lines_pct'))} | {_pct(d.get('branch_effective'))} | "
                         f"{d.get('branch_total', 0)} | {state} |")
        lines.append("")

    build = outcome.get("build") or {}
    diags = build.get("diagnostics") or []
    lines += ["## 编译诊断", "",
              f"> 告警 {len(build.get('warnings') or [])} 条 · "
              f"错误 {len(build.get('errors') or [])} 条", ""]
    if diags:
        lines += ["| 级别 | 文件 | 行 | 说明 |", "|---|---|---|---|"]
        for d in diags[:60]:
            lines.append(f"| {d['level']} | {d['file']} | {d['line']} | "
                         f"{str(d['msg']).replace('|', '/')} |")
        lines.append("")

    manifest = outcome.get("manifest") or {}
    if manifest:
        lines += ["## 输入指纹", "",
                  "> 哈希在同步前于编排侧计算，用于证明「跑的就是这份代码」。", "",
                  "| 文件 | 字节 | sha256 |", "|---|---|---|"]
        for path, info in sorted(manifest.items()):
            lines.append(f"| {path} | {info.get('bytes', 0)} | "
                         f"`{str(info.get('sha256', ''))[:16]}…` |")
        lines.append("")

    cmds = outcome.get("commands") or []
    if cmds:
        lines += ["## 执行命令", ""]
        for c in cmds:
            lines += [f"```sh", f"cd {c.get('cwd', '-')}", f"{c.get('cmd', '-')}",
                      f"# 退出码 {c.get('exit_code')}", "```"]
        lines.append("")

    raw = (outcome.get("raw_log") or "").strip()
    if raw:
        lines += ["## 原始输出", "", "```text", raw[-8000:], "```"]
    return "\n".join(lines)
