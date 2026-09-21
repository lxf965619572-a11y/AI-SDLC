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
from verification import targets as tgt_tab

# 原始日志入库上限：SQLite 里存全文便于追溯，但要防住异常输出把库撑爆
RAW_LOG_LIMIT = 200_000

# 单个目标的结论里另存为 <tid>.log 的大字段：结论文件（exec.json）只留可判定部分
_TARGET_LOG_KEYS = ("raw_log", "run_section", "coverage_section")


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
    出争议时要能拿出未截断的原始输出。

    多目标机时每个目标另存一份 `<目标id>.log`：汇总结论取的是各目标最差值，
    争议往往落在「到底是哪个目标上挂的」，那时要能单独拿出那个目标的原始输出。"""
    import config

    base = Path(config.VERIFY_EVIDENCE_DIR) / f"p{project_id}" / f"v{version}"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{stage}.log"
    try:
        path.write_text(outcome.get("raw_log") or "", encoding="utf-8")
        meta = base / f"{stage}.json"
        slim = {k: v for k, v in outcome.items() if k != "raw_log"}
        per_target = slim.get("target_results")
        if isinstance(per_target, dict) and per_target:
            slim["target_results"] = {
                tid: {k: v for k, v in (t or {}).items() if k not in _TARGET_LOG_KEYS}
                for tid, t in per_target.items()}
        meta.write_text(json.dumps(slim, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        ran = [tid for tid, t in (outcome.get("target_results") or {}).items()
               if (t or {}).get("ran")]
        # 只有多目标才另存 <目标id>.log：单目标时 exec.log 就是那个目标的完整原文，
        # 再写一份等于把同一批字节存两遍，还会让既有项目的证据目录凭空多出文件。
        if len(ran) > 1:
            for tid in ran:
                try:
                    (base / f"{tid}.log").write_text(
                        (outcome["target_results"][tid].get("raw_log") or ""),
                        encoding="utf-8")
                except OSError:
                    pass
    except OSError:
        return ""
    return str(path).replace("\\", "/")


# ---------------- 编译探针（代码 / 测试实现阶段的硬校验） ----------------
def _probe_one_target(runner, workdir: str, code_files: dict,
                      test_files: dict | None, link: bool, tgt, timeout) -> dict:
    """在一个目标机上做「能不能编（/能不能链）」的硬校验。"""
    files = buildkit.workspace_files(code_files, test_files, tgt)
    files.pop("build.sh", None)                 # 探针不运行、不采覆盖率
    files["check.sh"] = buildkit.check_script(link=link, tgt=tgt)
    # 探针目录每轮清空重来：上一轮的 .o 残留会让「其实编不过」的结论被掩盖
    runner.run(f"rm -rf {_shq(workdir)}", timeout=30)
    runner.sync(files, workdir)
    res = runner.run("sh check.sh", cwd=workdir, timeout=timeout)
    log = res.output
    ok = res.ok and "WB_BUILD_RESULT ok" in (res.stdout or "")
    if not ok and not log.strip():
        log = f"退出码 {res.exit_code}，无输出"
    return {"target": tgt.id, "ok": ok, "skipped": False, "exit_code": res.exit_code,
            "log": _clip(log, 60_000), "reason": "" if ok else "远端编译未通过",
            "workdir": workdir, "cmd": res.cmd}


def compile_probe(runner, project_id: int, code_files: dict,
                  test_files: dict | None = None, stage: str = "code",
                  timeout: int | None = None, targets=None) -> dict:
    """远端编译校验。返回 {"ok", "reason", "log", "exit_code", "skipped"}。

    test_files 为 None 时只编 src（代码阶段）；给了就连测试一起编并链接
    （测试实现阶段：链接通过才说明测试真的能跑起来）。
    runner 为 None 表示未配置验证机，此时不做远端编译，明确标 skipped——
    离线/mock 模式要能继续走，但绝不能把 skipped 当成 ok。

    targets 给了多个目标机时逐个校验，返回里多带 targets / per_target；
    某个目标的交叉工具链没装属于**环境问题**而不是代码问题：该目标记 skipped
    并在 unavailable 里点名，不触发重生（重生也变不出编译器），
    真正的拦截发生在执行验证节点——那里会因该目标未验证而转人工。"""
    link = test_files is not None
    if runner is None:
        return {"ok": True, "skipped": True, "exit_code": None, "log": "",
                "reason": "未配置验证机，跳过远端编译校验（WARN）"}

    try:
        tgts = tgt_tab.resolve(targets)
    except tgt_tab.TargetError as e:
        return {"ok": False, "skipped": False, "exit_code": None, "log": "",
                "reason": f"目标机配置错误：{e}", "targets": []}
    multi = len(tgts) > 1
    base_wd = f"{runner.workdir(project_id, 'probe')}/{stage}"
    cross = [t for t in tgts if not t.host]
    probed = tgt_tab.probe_targets(runner, cross) if cross else {}
    unavailable = tgt_tab.unavailable_reason(probed, cross) if cross else ""

    per_target: dict = {}
    order: list = []
    try:
        for t in tgts:
            wd = f"{base_wd}/{t.id}" if multi else base_wd
            info = probed.get(t.id)
            if info is not None and not info.get("ok"):
                per_target[t.id] = {
                    "target": t.id, "ok": True, "skipped": True, "exit_code": None,
                    "log": "", "workdir": wd,
                    "reason": "目标机工具链不可用（缺 "
                              f"{str(info.get('tools') or '交叉编译器/qemu').strip()}），"
                              "本阶段未校验"}
            else:
                per_target[t.id] = _probe_one_target(
                    runner, wd, code_files, test_files, link, t, timeout)
            order.append(t.id)
    except Exception as e:                       # 连不上、超时、tar 缺失
        return {"ok": False, "skipped": False, "exit_code": None, "log": "",
                "reason": f"验证机不可用：{e}"}

    checked = [tid for tid in order if not per_target[tid].get("skipped")]
    if not multi:
        one = per_target[order[0]]
        out = {"ok": bool(one.get("ok")), "skipped": bool(one.get("skipped")),
               "exit_code": one.get("exit_code"), "log": one.get("log") or "",
               "reason": one.get("reason") or "", "workdir": one.get("workdir"),
               "cmd": one.get("cmd")}
        if unavailable:
            out["unavailable"] = unavailable
        return out

    ok = all(bool(per_target[tid].get("ok")) for tid in checked) if checked else True
    bad = [tid for tid in checked if not per_target[tid].get("ok")]
    reason = "" if ok else "；".join(
        f"{tid}：{per_target[tid].get('reason')}" for tid in bad)
    if not checked and not reason:
        # 一个目标都没真编过：这既不是「通过」也不是「编译失败」，
        # 措辞必须说准，否则日志会把它写成代码问题去触发重生。
        reason = f"所有目标机均未校验（{unavailable or '工具链不可用'}）"
    return {
        "ok": ok, "skipped": not checked,
        "exit_code": next((per_target[tid].get("exit_code") for tid in bad), None),
        "log": _clip("\n".join(
            f"===== 目标机 {tid} =====\n{per_target[tid].get('log') or ''}"
            for tid in order if not per_target[tid].get("skipped")), 60_000),
        "reason": reason,
        "targets": order, "per_target": per_target,
        **({"unavailable": unavailable} if unavailable else {}),
    }


# ---------------- 完整验证运行 ----------------
_VERDICT_RANK = {VERDICT_OK: 0, VERDICT_SKIPPED: 1, VERDICT_FAIL: 2}


def _worst(values) -> str:
    """逐维取最差：fail > skipped > ok。

    多目标汇总只能用最差值，不能用多数表决：host 全过而 ppc32 挂一条，
    说明代码里藏着字节序假设——这正是换目标机要抓的东西，投票恰好会把它投掉。"""
    vals = [v for v in values if v]
    return max(vals, key=lambda v: _VERDICT_RANK.get(v, 1)) if vals else VERDICT_SKIPPED


def _verdict_of(build_ok: bool, tests: dict, coverage: dict) -> dict:
    """三维判定：构建不过则后两维只能是 skipped（没跑起来的东西谈不上通过率）。"""
    return {
        "build": VERDICT_OK if build_ok else VERDICT_FAIL,
        "tests": (VERDICT_OK if tests["all_pass"] else VERDICT_FAIL) if build_ok
                 else VERDICT_SKIPPED,
        "coverage": (VERDICT_OK if coverage["ok"] else VERDICT_FAIL) if build_ok
                    else VERDICT_SKIPPED,
    }


def _target_reason(build_ok: bool, no_source: bool, tests: dict, coverage: dict,
                   branch_min: float, line_min: float) -> str:
    if not build_ok:
        return "无源文件可编译" if no_source else "编译或链接失败"
    if not tests["all_pass"]:
        return f"{tests['failed']} 条用例失败" + (
            f"，{tests['missing']} 条未执行" if tests["missing"] else "")
    if not coverage["ok"]:
        return _coverage_reason(coverage, branch_min, line_min)
    return ""


def _run_one_target(runner, workdir: str, code_files: dict, test_files: dict,
                    tgt, case_ids, branch_min: float, line_min: float,
                    timeout) -> dict:
    """一个目标机上的一轮完整执行：同步 → 构建运行 → 采覆盖率 → 解析出该目标结论。

    每个目标各有一份完整结论（构建/用例/覆盖率/原始输出/环境指纹），
    汇总层只做「取最差」，不重新解释工具输出——判据只能有一个来源。"""
    try:
        manifest = runner.sync(buildkit.workspace_files(code_files, test_files, tgt),
                               workdir)
    except Exception as e:                       # 连不上、超时、tar 缺失
        why = f"同步到验证机失败：{e}"
        return {"target": tgt.id, "facts": tgt.facts(), "why": tgt.why,
                "status": "error", "ran": False, "available": True, "ok": False,
                "error": why, "reason": why, "workdir": workdir, "manifest": {},
                "verdict": {k: VERDICT_SKIPPED for k in ("build", "tests", "coverage")}}

    res = runner.run("sh build.sh", cwd=workdir, timeout=timeout)
    sections = parsers.parse_sections(res.stdout or "")
    build_log = sections.get("build", "")
    diags = parsers.parse_diagnostics(build_log)
    build_ok = ("WB_BUILD_RESULT ok" in build_log) and res.exit_code != buildkit.EXIT_BUILD_FAIL
    no_source = res.exit_code == buildkit.EXIT_NO_SOURCE
    tests = parsers.parse_test_output(sections.get("run", ""), expected_ids=case_ids)
    coverage = parsers.summarize_coverage(
        parsers.parse_gcov_multi(sections.get("coverage", "")),
        branch_min=branch_min, line_min=line_min)
    ok = build_ok and tests["all_pass"] and coverage["ok"]
    return {
        "target": tgt.id, "facts": tgt.facts(), "why": tgt.why,
        "status": "ok" if ok else "fail", "ran": True, "available": True,
        "ok": ok, "no_source": no_source,
        "reason": _target_reason(build_ok, no_source, tests, coverage,
                                 branch_min, line_min),
        "verdict": _verdict_of(build_ok, tests, coverage),
        "workdir": workdir, "manifest": manifest, "exit_code": res.exit_code,
        "transport": res.transport,
        "commands": [{"cmd": res.cmd, "cwd": res.cwd, "exit_code": res.exit_code,
                      "duration_s": round(res.duration_s, 3)}],
        "env": parsers.parse_kv(sections.get("env", "")),
        "build": {"ok": build_ok, "log": _clip(build_log, 60_000),
                  "diagnostics": diags,
                  "warnings": [d for d in diags if d["level"] == "warning"],
                  "errors": [d for d in diags if d["level"] == "error"]},
        "tests": tests,
        "coverage": coverage,
        "raw_log": _clip(res.output),
        "run_section": _clip(sections.get("run", ""), 60_000),
        "coverage_section": _clip(sections.get("coverage", ""), 60_000),
    }


def _merge_build(ran: dict) -> dict:
    """多目标构建结论合并：诊断逐条标注来自哪个目标，日志按目标分段拼接。"""
    diags, logs = [], []
    for tid, t in ran.items():
        b = t.get("build") or {}
        for d in b.get("diagnostics") or []:
            diags.append({**d, "target": tid})
        if (b.get("log") or "").strip():
            logs.append(f"===== 目标机 {tid} =====\n{b['log']}")
    return {"ok": bool(ran) and all((t.get("build") or {}).get("ok")
                                    for t in ran.values()),
            "log": _clip("\n".join(logs), 60_000), "diagnostics": diags,
            "warnings": [d for d in diags if d["level"] == "warning"],
            "errors": [d for d in diags if d["level"] == "error"]}


def _join_per_target(ran: dict, order: list, key: str) -> str:
    return _clip("\n".join(f"===== 目标机 {tid} =====\n{ran[tid].get(key) or ''}"
                           for tid in order if tid in ran))


def run_verification(runner, project_id: int, version,
                     code_files: dict, test_files: dict,
                     case_ids=None, branch_min: float = 80.0,
                     line_min: float = 0.0, timeout: int | None = None,
                     decision_cb=None, targets=None) -> dict:
    """跑一轮完整验证：同步 → 构建运行 → 覆盖率 → 解析 → 出结论。

    targets 为空时只在验证机本机跑，证据布局与结论形状与单目标时代完全一致；
    配了多个目标机则逐个交叉编译 + qemu 执行，结论按「取最差」汇总。
    工具链缺失的目标记 unavailable，整轮转人工（blocked）——
    「这个目标没测」绝不能被写成「这个目标通过」。"""
    t0 = time.time()
    base = {
        "project_id": project_id, "version": str(version),
        "duration_s": 0.0, "raw_log": "", "commands": [],
        "manifest": {}, "env": {}, "thresholds": {"branch_min": branch_min,
                                                   "line_min": line_min},
    }
    skipped_verdict = {"build": VERDICT_SKIPPED, "tests": VERDICT_SKIPPED,
                       "coverage": VERDICT_SKIPPED}

    def _blocked(reason: str, **extra) -> dict:
        return {**base, **extra, "ok": False, "skipped": True,
                "decision": DECISION_BLOCKED, "reason": reason,
                "verdict": dict(skipped_verdict)}

    try:
        tgts = tgt_tab.resolve(targets)
    except tgt_tab.TargetError as e:
        return _blocked(f"目标机配置错误：{e}")
    order = [t.id for t in tgts]
    multi = len(tgts) > 1

    if runner is None:
        return _blocked("未配置验证机（VERIFY_HOST 为空），无法执行编译与测试",
                        targets=order)

    try:
        probe = runner.probe()
    except Exception as e:
        return _blocked(f"验证机探测失败：{e}", targets=order)
    if not probe.get("ok"):
        return _blocked(f"验证机工具链不可用：{probe.get('error') or '未知原因'}",
                        env=probe, targets=order)

    # host 的工具链已由 runner.probe 覆盖，只有交叉目标才额外探测：
    # 单目标路径不产生任何新的远端调用，既有证据与耗时一字不变。
    cross = [t for t in tgts if not t.host]
    probed = tgt_tab.probe_targets(runner, cross) if cross else {}

    base_wd = runner.workdir(project_id, version)
    per_target: dict = {}
    for t in tgts:
        wd = f"{base_wd}/{t.id}" if multi else base_wd
        info = probed.get(t.id)
        if info is not None and not info.get("ok"):
            per_target[t.id] = {
                "target": t.id, "facts": t.facts(), "why": t.why,
                "status": "unavailable", "ran": False, "available": False,
                "ok": False, "probe": info, "workdir": wd, "manifest": {},
                "reason": "目标机工具链不可用（缺 "
                          f"{str(info.get('tools') or '交叉编译器/qemu').strip()}）",
                "verdict": dict(skipped_verdict)}
            continue
        per_target[t.id] = _run_one_target(runner, wd, code_files, test_files, t,
                                           case_ids, branch_min, line_min, timeout)

    ran = {tid: per_target[tid] for tid in order if per_target[tid].get("ran")}
    dead = {tid: per_target[tid] for tid in order if not per_target[tid].get("ran")}
    if not ran:
        only = per_target[order[0]]
        return _blocked(only.get("error") or only.get("reason") or "所有目标机均无法执行",
                        env=probe, workdir=base_wd, targets=order,
                        target_results=per_target,
                        **({"target_probe": probed} if probed else {}))

    # 顶层输入指纹一律按 host 构建脚本计算：它代表「源码基线」，与目标无关，
    # 软件工程包据此逐文件对齐；各目标真实同步的构建脚本指纹在 target_results 里。
    manifest = runner.manifest(buildkit.workspace_files(code_files, test_files))

    if not multi:
        one = ran[order[0]]
        build, tests, coverage = one["build"], one["tests"], one["coverage"]
        verdict = one["verdict"]
        env = {**{k: probe.get(k) for k in ("uname", "cores", "gcc", "gcov", "make", "tar")
                  if probe.get(k)}, **one.get("env", {})}
        commands = list(one.get("commands") or [])
        raw = one.get("raw_log") or ""
        run_section = one.get("run_section") or ""
        coverage_section = one.get("coverage_section") or ""
        exit_code = one.get("exit_code")
        transport = one.get("transport")
        workdir = one.get("workdir")
        ok = bool(one.get("ok"))
        reason = one.get("reason") or ""
    else:
        build_tids = [tid for tid in order
                      if tid in ran and (ran[tid].get("build") or {}).get("ok")]
        build = _merge_build(ran)
        tests = (parsers.merge_tests({tid: ran[tid]["tests"] for tid in build_tids})
                 if build_tids
                 else parsers.parse_test_output("", expected_ids=case_ids))
        cov_src = {tid: ran[tid]["coverage"] for tid in build_tids
                   if (ran[tid]["coverage"].get("functions")
                       or (ran[tid]["coverage"].get("totals") or {}).get("lines_total"))}
        coverage = (parsers.merge_coverage(cov_src, branch_min=branch_min,
                                           line_min=line_min) if cov_src
                    else parsers.summarize_coverage({}, branch_min=branch_min,
                                                    line_min=line_min))
        verdict = {k: _worst([ran[tid]["verdict"].get(k) for tid in ran])
                   for k in ("build", "tests", "coverage")}
        env = {**{k: probe.get(k) for k in ("uname", "cores", "make", "tar")
                  if probe.get(k)},
               "gcc": " | ".join(f"{tid}: {(ran[tid].get('env') or {}).get('gcc') or '-'}"
                                 for tid in order if tid in ran),
               "gcov": " | ".join(f"{tid}: {(ran[tid].get('env') or {}).get('gcov') or '-'}"
                                  for tid in order if tid in ran),
               "targets": ",".join(order),
               "target_env": {tid: ran[tid].get("env") or {}
                              for tid in order if tid in ran}}
        if probed:
            env["target_probe"] = probed
        commands = [dict(c, target=tid) for tid in order if tid in ran
                    for c in (ran[tid].get("commands") or [])]
        raw = _join_per_target(ran, order, "raw_log")
        run_section = _join_per_target(ran, order, "run_section")
        coverage_section = _join_per_target(ran, order, "coverage_section")
        exit_code = next((ran[tid].get("exit_code") for tid in order
                          if tid in ran and ran[tid].get("exit_code") not in (0, None)), 0)
        transport = next((ran[tid].get("transport") for tid in order if tid in ran), "")
        workdir = base_wd
        bad = [tid for tid in order if not per_target[tid].get("ok")]
        ok = not bad
        reason = (f"{len(bad)}/{len(order)} 个目标机未通过：" + "；".join(
            f"{tid}：{per_target[tid].get('reason') or per_target[tid].get('error') or '未知原因'}"
            for tid in bad)) if bad else ""

    if dead:
        decision = DECISION_BLOCKED
    else:
        decision = DECISION_NEXT if ok else DECISION_FIX_CODE
        if not ok and decision_cb is not None:
            try:
                decision, reason = decision_cb(outcome_reason=reason, tests=tests,
                                               coverage=coverage,
                                               build_ok=build["ok"],
                                               diags=build["diagnostics"]) or (decision, reason)
            except Exception:
                pass

    outcome = {
        **base,
        "ok": ok, "skipped": False, "decision": decision, "reason": reason,
        "verdict": verdict, "env": env, "manifest": manifest,
        "workdir": workdir, "exit_code": exit_code,
        "duration_s": round(time.time() - t0, 3),
        "transport": transport,
        "commands": commands,
        "synced_files": sorted(manifest.keys()),
        "build": build,
        "tests": tests,
        "coverage": coverage,
        "raw_log": raw,
        "run_section": run_section,
        "coverage_section": coverage_section,
        "targets": order,
        "target_results": per_target,
    }
    if probed:
        outcome["target_probe"] = probed
    if not multi:
        real = ran[order[0]].get("manifest") or {}
        if real and real != manifest:
            outcome["manifest_note"] = (
                "顶层输入指纹按 host 构建脚本计算（用于与源码基线对齐）；"
                f"实际同步到 {order[0]} 的构建脚本指纹见 target_results."
                f"{order[0]}.manifest")
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
    哪条用例失败以及失败时打印了什么、哪些函数根本没被执行到。

    多目标机时先给逐目标一行结论，再只展开失败目标的明细——归因智能体第一件
    要判断的事就是「所有目标都挂还是只有某个架构挂」，这个信息必须放在最前面。"""
    lines = []
    verdict = outcome.get("verdict") or {}
    lines.append(f"判定：构建 {verdict.get('build')} · 用例 {verdict.get('tests')} · "
                 f"覆盖率 {verdict.get('coverage')}")
    if outcome.get("reason"):
        lines.append(f"结论：{outcome['reason']}")

    per_target = outcome.get("target_results") or {}
    if len(per_target) > 1:
        lines.append("目标机逐项：")
        for tid, t in per_target.items():
            v = t.get("verdict") or {}
            note = "" if t.get("ok") else f"（{t.get('reason') or t.get('error')}）"
            lines.append(f"- {tid}：构建 {v.get('build')} · 用例 {v.get('tests')} · "
                         f"覆盖率 {v.get('coverage')}{note}")
        for tid, t in per_target.items():
            if t.get("ok") or not t.get("ran"):
                continue
            lines += ["", f"--- 目标机 {tid} 明细 ---"]
            lines += _brief_one(t.get("build") or {}, t.get("tests") or {},
                                t.get("coverage") or {}, limit)
        return "\n".join(lines)

    lines += _brief_one(outcome.get("build") or {}, outcome.get("tests") or {},
                        outcome.get("coverage") or {}, limit)
    return "\n".join(lines)


def _brief_one(build: dict, tests: dict, cov: dict, limit: int = 20) -> list:
    """一份（可能是某个目标机的）结论 → 明细行。单目标与多目标共用同一套措辞。"""
    lines = []
    for d in (build.get("diagnostics") or [])[:limit]:
        lines.append(f"[{d['level']}] {d['file']}:{d['line']}: {d['msg']}")
    if not build.get("ok"):
        tail = (build.get("log") or "").strip().splitlines()[-15:]
        lines += [f"构建日志尾部：{ln}" for ln in tail]

    for r in (tests.get("results") or []):
        if r["status"] != parsers.ST_PASS:
            lines.append(f"[{r['status'].upper()}] {r['id']} {r.get('detail','')}")
    if tests.get("crashed"):
        lines.append(f"测试进程异常退出，exit={tests.get('run_exit')}")
    if tests.get("consistent") is False:
        lines.append("测试桩自报的用例数与实际输出不一致，测试代码本身可能有问题")

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
    return lines


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

    多目标机时多一条确定性判据，而且它排在最前面：本机全过、只有交叉目标挂，
    那就是可移植性缺陷，责任必然在被测代码——这类问题交给模型读代码反而容易
    被判成「测试期望值写错了」。
    """
    if outcome.get("skipped"):
        return DECISION_BLOCKED, outcome.get("reason") or "验证机不可用"

    per_target = outcome.get("target_results") or {}
    if len(per_target) > 1:
        return _decide_multi(per_target)
    return _decide_single(outcome.get("build") or {}, outcome.get("tests") or {},
                          outcome.get("coverage") or {})


def _is_host_target(target_id: str) -> bool:
    t = tgt_tab.by_id(target_id)
    return bool(t and t.host)


def _decide_multi(per_target: dict) -> tuple[str, str] | None:
    """多目标归因：先看有没有目标根本没跑，再看是不是「只有交叉目标挂」。"""
    dead = {tid: t for tid, t in per_target.items() if not t.get("ran")}
    if dead:
        return DECISION_BLOCKED, ("目标机未全部执行，结论不完整：" + "；".join(
            f"{tid}：{t.get('reason') or t.get('error') or '未执行'}"
            for tid, t in dead.items()))

    bad = {tid: t for tid, t in per_target.items() if not t.get("ok")}
    if not bad:
        return None

    host_ids = [tid for tid in per_target if _is_host_target(tid)]
    host_green = all(per_target[tid].get("ok") for tid in host_ids)
    cross_only = all(not _is_host_target(tid) for tid in bad)
    case_level = all((t.get("build") or {}).get("ok")
                     and ((t.get("tests") or {}).get("failed")
                          or (t.get("tests") or {}).get("missing"))
                     for t in bad.values())
    if cross_only and case_level and (host_green or not host_ids):
        detail = "；".join(f"{tid}：{bad[tid].get('reason')}" for tid in bad)
        scope = ("验证机本机全部通过，" if host_ids else "其余目标机通过，")
        return DECISION_FIX_CODE, (
            f"同一份源码{scope}只在交叉目标上失败（{detail}）。"
            "判为可移植性缺陷：字长 / 字节序 / 对齐 / 整型宽度假设，"
            "责任在被测代码，不得靠修改测试判据绕过。")

    decided = {tid: _decide_single(t.get("build") or {}, t.get("tests") or {},
                                   t.get("coverage") or {})
               for tid, t in bad.items()}
    if all(decided.values()) and len({d[0] for d in decided.values()}) == 1:
        kind = next(iter(decided.values()))[0]
        detail = "；".join(f"{tid}：{decided[tid][1]}" for tid in bad)
        return kind, f"{len(bad)} 个目标机归因一致（{kind}）——{detail}"
    return None


def _decide_single(build: dict, tests: dict, cov: dict) -> tuple[str, str] | None:
    """单个目标上的确定性归因判据（多目标时对每个失败目标各跑一次）。"""
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

    if tests.get("consistent") is False:
        return DECISION_FIX_TEST, (
            f"测试桩自报 total={tests.get('reported_total')} "
            f"failed={tests.get('reported_failed')}，与实际解析出的 "
            f"{tests.get('total')} 条不一致，测试代码的结果上报有误")

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
    per_target = outcome.get("target_results") or {}
    multi = len(per_target) > 1
    lines += ["## 执行环境", "",
              "| 项 | 值 |", "|---|---|",
              f"| 传输方式 | {outcome.get('transport', '-')} |",
              f"| 验证机 | {env.get('uname', '-')} |",
              f"| 编译器 | {env.get('gcc', '-')} |",
              f"| 覆盖率工具 | {env.get('gcov', '-')} |",
              f"| 远端工作目录 | {outcome.get('workdir', '-')} |",
              f"| 编译选项 | {buildkit.CFLAGS} |",
              f"| 耗时 | {outcome.get('duration_s', 0)} s |", ""]

    if multi:
        lines += ["## 目标机矩阵", "",
                  "> 同一份源码在每个目标机上分别交叉编译并执行，汇总结论取各目标最差值："
                  "任一目标不通过即整体不通过。工具链缺失的目标记为「未执行」，"
                  "整轮转人工裁决，不会被静默放行。", "",
                  "| 目标 | 字长/字节序 | 编译器 | 执行方式 | 构建 | 用例 | 覆盖率 | 说明 |",
                  "|---|---|---|---|---|---|---|---|"]
        for tid, t in per_target.items():
            f = t.get("facts") or {}
            v = t.get("verdict") or {}
            run = f"`{f.get('qemu')}`" if f.get("qemu") else "本机直接执行"
            endian = "大端" if f.get("endian") == "big" else "小端"
            note = (t.get("reason") or t.get("error")
                    or ("通过" if t.get("ok") else "未通过"))
            lines.append(f"| {tid} · {f.get('label') or '-'} "
                         f"| {f.get('bits', '-')} 位 / {endian} "
                         f"| `{f.get('cc') or '-'}` | {run} "
                         f"| {mark.get(v.get('build'), '-')} "
                         f"| {mark.get(v.get('tests'), '-')} "
                         f"| {mark.get(v.get('coverage'), '-')} | {note} |")
        lines.append("")

    tests = outcome.get("tests") or {}
    results = tests.get("results") or []
    lines += ["## 用例执行结果", "",
              f"> 共 {tests.get('total', 0)} 条：通过 {tests.get('passed', 0)} · "
              f"失败 {tests.get('failed', 0)} · 未执行 {tests.get('missing', 0)}", ""]
    if results:
        if multi:
            lines += ["| 用例 | 结果 | 各目标机 | 说明 |", "|---|---|---|---|"]
            for r in results:
                detail = str(r.get("detail") or "").replace("|", "/").replace("\n", " ")
                per = "、".join(f"{k} {v}" for k, v in (r.get("per_target") or {}).items())
                lines.append(f"| {r['id']} | {r.get('status_text', r['status'])} "
                             f"| {per} | {detail} |")
        else:
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
