"""验证执行编排（executor）的测试：编译探针 / 完整验证 / 归因决策 / 报告 / 证据归档。

第一性原理：executor 是整套系统「下结论」的那一层，它的每一个返回值都会被当成
交付证据折回需求追溯矩阵。所以这里钉的不是「函数能不能跑」，而是三条判据契约：
  1. 未配置验证机时必须是 skipped，绝不能退化成 ok——离线能走，但假绿不行；
  2. 构建 / 用例 / 覆盖率三维判定互相独立，任一不过则总判定不过，且原因说准确；
  3. 归因（auto_decision）凡有确定判据的必须判死方向，只有真正需要读代码权衡的
     情形（用例挂了但构建与覆盖率都正常）才返回 None 交给归因智能体。

全部用 verification.runner.FakeRunner 替身，不连真实 SSH、不碰真实进程与网络，
因此本文件零第三方依赖、可离线运行：
    .venv\\Scripts\\python.exe tests\\test_executor.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from verification import buildkit, executor, parsers
from verification import targets as T
from verification.runner import FakeRunner, RunResult
from tests.fixtures import comm_sample as SAMPLE

CASE_IDS = SAMPLE.CASE_IDS


# ---------- 夹具：拼出 build.sh 的分段 stdout ----------
def _gcov_function(name, lines_total, lines_pct, branch_total, branch_pct):
    out = [f"Function '{name}'",
           f"Lines executed:{lines_pct:.2f}% of {lines_total}"]
    if branch_total:
        out.append(f"Branches executed:{branch_pct:.2f}% of {branch_total}")
        out.append(f"Taken at least once:{branch_pct:.2f}% of {branch_total}")
    else:
        out.append("No branches")
    out.append("No calls")
    return "\n".join(out)


# 六个被测函数，对应样例模块的设计基线
_FUNCS_GREEN = [
    ("comm_crc16", 12, 100.0, 4, 100.0),
    ("comm_init", 8, 100.0, 2, 100.0),
    ("comm_pack", 20, 100.0, 6, 100.0),
    ("comm_unpack", 24, 100.0, 8, 100.0),
    ("comm_ack", 10, 100.0, 4, 100.0),
    ("comm_next_retransmit", 8, 100.0, 2, 100.0),
]
# 覆盖率不达标：comm_pack 分支只覆盖 50%，文件级 60%，均低于 80% 门限
_FUNCS_LOW = [
    ("comm_crc16", 12, 100.0, 4, 100.0),
    ("comm_init", 8, 100.0, 2, 100.0),
    ("comm_pack", 20, 100.0, 6, 50.0),
    ("comm_unpack", 24, 100.0, 8, 100.0),
    ("comm_ack", 10, 100.0, 4, 100.0),
    ("comm_next_retransmit", 8, 100.0, 2, 100.0),
]


def _coverage_section(funcs, file_branch_pct, file_branch_total):
    body = "\n\n".join(_gcov_function(*f) for f in funcs)
    file_block = (
        "File 'src/comm.c'\n"
        "Lines executed:100.00% of 82\n"
        f"Branches executed:{file_branch_pct:.2f}% of {file_branch_total}\n"
        f"Taken at least once:{file_branch_pct:.2f}% of {file_branch_total}\n"
        "Calls executed:100.00% of 6\n"
        "Creating 'comm.c.gcov'")
    return f"WB_GCOV_TARGET src/comm.c\n{body}\n\n{file_block}"


def _run_section(case_ids, fails=(), omit=(), run_exit=0, summary=True,
                 total_override=None, failed_override=None):
    lines = ["WB_BEGIN"]
    nfail = printed = 0
    for cid in case_ids:
        if cid in omit:
            continue
        printed += 1
        if cid in fails:
            lines.append(f"{cid} FAIL 断言未通过：期望值不符")
            nfail += 1
        else:
            lines.append(f"{cid} PASS")
    if summary:
        total = printed if total_override is None else total_override
        failed = nfail if failed_override is None else failed_override
        lines.append(f"WB_TOTAL {total}")
        lines.append(f"WB_FAILED {failed}")
        lines.append("WB_END")
    lines.append(f"WB_RUN_EXIT {run_exit}")
    return "\n".join(lines)


def _stdout(case_ids, fails=(), omit=(), build_ok=True, exit_code=0,
            cov="green", diags=None, run_exit=0, summary=True,
            total_override=None, failed_override=None,
            target="host", cwd=None):
    """拼出 build.sh 的完整分段输出（env/build/run/coverage/end）。

    target 决定 env 段的目标指纹。真实 build.sh 的编译器名、qemu、编译选项
    都由 buildkit 从目标表注入，夹具必须走同一张表——否则测的是夹具自己
    编出来的假环境，目标表改错了这些测试照样全绿。
    默认 target="host" 时各字段与多目标改造之前一字不变。"""
    tgt = T.by_id(target) or T.by_id("host")
    env = "\n".join([
        "uname=Linux 4.15.0-156-generic x86_64", "cores=4",
        f"target={tgt.id}",
        f"qemu={tgt.qemu or 'none'}",
        f"gcc={tgt.cc} (Ubuntu 7.5.0-3ubuntu1~18.04) 7.5.0",
        f"gcov={tgt.gcov} (Ubuntu 7.5.0-3ubuntu1~18.04) 7.5.0",
        f"cwd={cwd or '/fake/wb_verify/p1/v1'}",
        "cflags=" + buildkit.cflags_for(tgt)])
    build_lines = list(diags or [])
    build_lines.append("WB_BUILD_RESULT ok" if build_ok else "WB_BUILD_RESULT fail")
    parts = ["WB_SECTION env", env, "WB_SECTION build", "\n".join(build_lines)]
    if build_ok:
        funcs = _FUNCS_GREEN if cov == "green" else _FUNCS_LOW
        fpct, ftot = (100.0, 26) if cov == "green" else (60.0, 20)
        parts += ["WB_SECTION run",
                  _run_section(case_ids, fails, omit, run_exit, summary,
                               total_override, failed_override),
                  "WB_SECTION coverage", _coverage_section(funcs, fpct, ftot)]
    parts += ["WB_SECTION end"]
    return "\n".join(parts)


def _runner(stdout, exit_code=0, probe=None):
    return FakeRunner(responses={"sh build.sh": {"exit_code": exit_code,
                                                 "stdout": stdout}},
                      probe=probe)


def _green_outcome(**kw):
    r = _runner(_stdout(CASE_IDS, **kw))
    return executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                                     case_ids=CASE_IDS, branch_min=80.0)


# ================= compile_probe =================
def test_compile_probe_no_runner_skips_but_not_ok():
    """未配置验证机：明确 skipped，ok=True 只是「不阻断离线流程」，绝非「编译通过」。"""
    out = executor.compile_probe(None, 1, SAMPLE.CODE_FILES)
    assert out["skipped"] is True
    assert out["ok"] is True
    assert out["exit_code"] is None
    assert "验证机" in out["reason"]


def test_compile_probe_code_only_ok():
    r = FakeRunner(responses={"sh check.sh": {"exit_code": 0,
                                              "stdout": "WB_BUILD_RESULT ok\n"}})
    out = executor.compile_probe(r, 1, SAMPLE.CODE_FILES, stage="code")
    assert out["ok"] is True and out["skipped"] is False
    assert out["exit_code"] == 0 and out["reason"] == ""


def test_compile_probe_drops_build_sh_and_adds_check_sh():
    """探针只判「能不能编」，不运行、不采覆盖率，所以工作区里没有 build.sh。"""
    r = FakeRunner(responses={"sh check.sh": {"exit_code": 0,
                                              "stdout": "WB_BUILD_RESULT ok\n"}})
    executor.compile_probe(r, 1, SAMPLE.CODE_FILES, stage="code")
    workdir = f"{r.workdir(1, 'probe')}/code"
    synced = set(r.files[workdir].keys())
    assert "build.sh" not in synced
    assert "check.sh" in synced
    assert "src/comm.c" in synced and "include/comm.h" in synced


def test_compile_probe_link_stage_embeds_link_block():
    """测试实现阶段带链接：check.sh 里必须真的去链接成 wb_tests，链接过才算通过。"""
    r = FakeRunner(responses={"sh check.sh": {"exit_code": 0,
                                              "stdout": "WB_BUILD_RESULT ok\n"}})
    executor.compile_probe(r, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                           stage="test_impl")
    workdir = f"{r.workdir(1, 'probe')}/test_impl"
    check_sh = r.files[workdir]["check.sh"].decode("utf-8")
    assert "-o build/wb_tests" in check_sh


def test_compile_probe_build_fail():
    r = FakeRunner(responses={"sh check.sh": {
        "exit_code": 2,
        "stdout": "WB_BUILD_RESULT fail\nsrc/comm.c:10:5: error: 'x' undeclared\n"}})
    out = executor.compile_probe(r, 1, SAMPLE.CODE_FILES)
    assert out["ok"] is False and out["skipped"] is False
    assert out["exit_code"] == 2
    assert out["reason"] == "远端编译未通过"
    assert "undeclared" in out["log"]


def test_compile_probe_fail_without_output_is_explained():
    """编不过又没有任何输出时，日志不能是空串——否则归因无从下手。"""
    r = FakeRunner(responses={"sh check.sh": {"exit_code": 2, "stdout": ""}})
    out = executor.compile_probe(r, 1, SAMPLE.CODE_FILES)
    assert out["ok"] is False
    assert "退出码 2" in out["log"]


def test_compile_probe_clears_workdir_first():
    """探针目录每轮先清空：上一轮 .o 残留会把「其实编不过」掩盖成「通过」。"""
    r = FakeRunner(responses={"sh check.sh": {"exit_code": 0,
                                              "stdout": "WB_BUILD_RESULT ok\n"}})
    executor.compile_probe(r, 1, SAMPLE.CODE_FILES)
    assert r.calls[0]["cmd"].startswith("rm -rf")


def test_compile_probe_runner_unavailable():
    """sync/run 抛异常（连不上、tar 缺失、超时）时判不可用，且不谎报 skipped。"""
    class Boom(FakeRunner):
        def sync(self, files, remote_dir):
            raise RuntimeError("tar 管道被拦截")
    out = executor.compile_probe(Boom(), 1, SAMPLE.CODE_FILES)
    assert out["ok"] is False and out["skipped"] is False
    assert out["reason"].startswith("验证机不可用")


# ================= run_verification =================
def test_run_verification_no_runner_is_blocked_not_ok():
    out = executor.run_verification(None, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES)
    assert out["skipped"] is True and out["ok"] is False
    assert out["decision"] == executor.DECISION_BLOCKED
    assert out["verdict"] == {"build": executor.VERDICT_SKIPPED,
                              "tests": executor.VERDICT_SKIPPED,
                              "coverage": executor.VERDICT_SKIPPED}


def test_run_verification_probe_toolchain_missing_is_blocked():
    r = FakeRunner(probe={"ok": False, "error": "gcc not found"})
    out = executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES)
    assert out["skipped"] is True and out["decision"] == executor.DECISION_BLOCKED
    assert "工具链" in out["reason"]


def test_run_verification_all_green():
    out = _green_outcome()
    assert out["ok"] is True and out["skipped"] is False
    assert out["decision"] == executor.DECISION_NEXT
    assert out["verdict"] == {"build": executor.VERDICT_OK,
                              "tests": executor.VERDICT_OK,
                              "coverage": executor.VERDICT_OK}
    assert out["tests"]["all_pass"] is True and out["tests"]["missing"] == 0
    assert out["coverage"]["ok"] is True
    assert out["workdir"] == "/fake/wb_verify/p1/v1"
    assert out["exit_code"] == 0


def test_run_verification_captures_env_and_manifest():
    """结论要能当证据：环境指纹与输入指纹必须随结论一起带回。"""
    out = _green_outcome()
    assert out["env"].get("uname", "").startswith("Linux")
    assert "7.5.0" in out["env"].get("gcc", "")
    assert out["env"].get("cwd") == "/fake/wb_verify/p1/v1"
    synced = set(out["synced_files"])
    for need in ("build.sh", "src/comm.c", "include/comm.h",
                 "tests/test_comm.c", "tests/wb_harness.c"):
        assert need in synced, need
    # 输入哈希在同步前于编排侧算好，证明「跑的就是这份代码」
    assert out["manifest"]["src/comm.c"]["sha256"]
    assert out["manifest"]["src/comm.c"]["bytes"] > 0


def test_run_verification_failing_test():
    out = _green_outcome(fails=("TC-003",))
    assert out["ok"] is False
    assert out["verdict"]["tests"] == executor.VERDICT_FAIL
    assert out["verdict"]["build"] == executor.VERDICT_OK
    assert out["tests"]["failed"] == 1
    assert "1 条用例失败" in out["reason"]
    # 节点层的粗判默认回代码；真正的方向由 auto_decision 决定（见下）
    assert out["decision"] == executor.DECISION_FIX_CODE


def test_run_verification_low_coverage():
    out = _green_outcome(cov="low")
    assert out["ok"] is False
    assert out["verdict"]["coverage"] == executor.VERDICT_FAIL
    assert out["verdict"]["tests"] == executor.VERDICT_OK
    assert out["tests"]["all_pass"] is True
    assert "覆盖率未达门限" in out["reason"]
    assert out["coverage"]["totals"]["branch_pct"] < 80.0


def test_run_verification_build_fail_skips_downstream():
    r = _runner(_stdout(CASE_IDS, build_ok=False, exit_code=2,
                        diags=["src/comm.c:10:5: error: 'x' undeclared"]),
                exit_code=2)
    out = executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                                    case_ids=CASE_IDS, branch_min=80.0)
    assert out["ok"] is False
    assert out["verdict"] == {"build": executor.VERDICT_FAIL,
                              "tests": executor.VERDICT_SKIPPED,
                              "coverage": executor.VERDICT_SKIPPED}
    assert out["reason"] == "编译或链接失败"
    assert out["build"]["errors"] and out["build"]["errors"][0]["line"] == 10


def test_run_verification_missing_case_marks_crash():
    """进程中途崩溃：没打结果的用例记 missing，且据 WB_RUN_EXIT 判 crashed。"""
    r = _runner(_stdout(CASE_IDS, omit=("TC-019",), summary=False, run_exit=139))
    out = executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                                    case_ids=CASE_IDS, branch_min=80.0)
    assert out["tests"]["missing"] == 1
    assert out["tests"]["crashed"] is True
    assert out["tests"]["all_pass"] is False
    assert "未执行" in out["reason"]


def test_run_verification_decision_cb_override():
    """节点可注入决策回调覆盖粗判（例如把覆盖率不足定向到测试侧）。"""
    r = _runner(_stdout(CASE_IDS, cov="low"))
    out = executor.run_verification(
        r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES, case_ids=CASE_IDS,
        branch_min=80.0, decision_cb=lambda **kw: (executor.DECISION_FIX_TEST, "定向到测试"))
    assert out["decision"] == executor.DECISION_FIX_TEST
    assert out["reason"] == "定向到测试"


# ================= auto_decision =================
def test_auto_decision_skipped_is_blocked():
    out = executor.run_verification(None, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES)
    assert executor.auto_decision(out) == (executor.DECISION_BLOCKED, out["reason"])


def test_auto_decision_build_fail_in_src_is_fix_code():
    r = _runner(_stdout(CASE_IDS, build_ok=False, exit_code=2,
                        diags=["src/comm.c:10:5: error: 'x' undeclared"]), exit_code=2)
    out = executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                                    case_ids=CASE_IDS, branch_min=80.0)
    decision, reason = executor.auto_decision(out)
    assert decision == executor.DECISION_FIX_CODE
    assert "src/comm.c:10" in reason


def test_auto_decision_build_fail_in_tests_is_fix_test():
    r = _runner(_stdout(CASE_IDS, build_ok=False, exit_code=2,
                        diags=["tests/test_comm.c:5:1: error: expected ';'"]), exit_code=2)
    out = executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                                    case_ids=CASE_IDS, branch_min=80.0)
    decision, reason = executor.auto_decision(out)
    assert decision == executor.DECISION_FIX_TEST
    assert "tests/test_comm.c:5" in reason


def test_auto_decision_undefined_reference_is_fix_test():
    """链接期符号未定义：诊断行不带 warning/error 级别，落点判据失效，靠关键字兜底。"""
    r = _runner(_stdout(CASE_IDS, build_ok=False, exit_code=2,
                        diags=["tests/test_comm.c:42: undefined reference to 'comm_pack'"]),
                exit_code=2)
    out = executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                                    case_ids=CASE_IDS, branch_min=80.0)
    decision, reason = executor.auto_decision(out)
    assert decision == executor.DECISION_FIX_TEST
    assert "链接失败" in reason


def test_auto_decision_multiple_definition_is_fix_test():
    r = _runner(_stdout(CASE_IDS, build_ok=False, exit_code=2,
                        diags=["multiple definition of 'comm_pack'"]), exit_code=2)
    out = executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                                    case_ids=CASE_IDS, branch_min=80.0)
    decision, _ = executor.auto_decision(out)
    assert decision == executor.DECISION_FIX_TEST


def test_auto_decision_inconsistent_harness_is_fix_test():
    """测试桩自报条数与实际解析对不上：全绿可能是假结论，责任在测试代码。"""
    r = _runner(_stdout(CASE_IDS, fails=("TC-003",), failed_override=5))
    out = executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                                    case_ids=CASE_IDS, branch_min=80.0)
    assert out["tests"]["consistent"] is False
    decision, reason = executor.auto_decision(out)
    assert decision == executor.DECISION_FIX_TEST
    assert "上报" in reason


def test_auto_decision_all_pass_low_cov_is_fix_test():
    out = _green_outcome(cov="low")
    decision, reason = executor.auto_decision(out)
    assert decision == executor.DECISION_FIX_TEST
    assert "缺的是用例不是实现" in reason
    assert "comm_pack" in reason


def test_auto_decision_genuine_test_fail_returns_none():
    """用例挂了、构建与覆盖率都正常：工具链判不出方向，必须交给归因智能体。"""
    out = _green_outcome(fails=("TC-003",))
    assert executor.auto_decision(out) is None


# ================= failure_brief =================
def test_failure_brief_lists_failed_case_and_verdict():
    out = _green_outcome(fails=("TC-003",))
    brief = executor.failure_brief(out)
    assert "判定：构建 ok" in brief
    assert "[FAIL] TC-003" in brief
    assert "总体：" in brief


def test_failure_brief_surfaces_untested_and_below_branch():
    out = _green_outcome(cov="low")
    brief = executor.failure_brief(out)
    assert "分支覆盖不足的函数：comm_pack" in brief


def test_failure_brief_includes_build_tail_on_fail():
    r = _runner(_stdout(CASE_IDS, build_ok=False, exit_code=2,
                        diags=["src/comm.c:10:5: error: 'x' undeclared"]), exit_code=2)
    out = executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                                    case_ids=CASE_IDS, branch_min=80.0)
    brief = executor.failure_brief(out)
    assert "[error] src/comm.c:10" in brief
    assert "构建日志尾部" in brief


# ================= markdown_report =================
def test_markdown_report_green():
    md = executor.markdown_report(_green_outcome())
    assert "# 代码验证执行报告" in md
    assert "全部通过" in md
    assert "comm_pack" in md          # 覆盖率函数表
    assert "## 输入指纹" in md


def test_markdown_report_fail_shows_failing_case():
    md = executor.markdown_report(_green_outcome(fails=("TC-003",)))
    assert "未通过" in md
    assert "TC-003" in md


def test_markdown_report_skipped():
    out = executor.run_verification(None, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES)
    md = executor.markdown_report(out)
    assert "未执行" in md and "VERIFY_HOST" in md


# ================= archive_evidence =================
def test_archive_evidence_writes_full_log_and_slim_meta():
    """落盘的是未截断全文 + 去掉 raw_log 的结论；出争议时要拿得出原始输出。"""
    out = _green_outcome()
    out["raw_log"] = "RAW-LOG-SENTINEL"
    with tempfile.TemporaryDirectory() as tmp:
        saved = config.VERIFY_EVIDENCE_DIR
        config.VERIFY_EVIDENCE_DIR = tmp
        try:
            rel = executor.archive_evidence(out, 7, "1t1")
        finally:
            config.VERIFY_EVIDENCE_DIR = saved
        assert rel.endswith("p7/v1t1/exec.log")
        log_path = Path(tmp) / "p7" / "v1t1" / "exec.log"
        meta_path = Path(tmp) / "p7" / "v1t1" / "exec.json"
        assert log_path.read_text(encoding="utf-8") == "RAW-LOG-SENTINEL"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "raw_log" not in meta
        assert meta["verdict"]["build"] == executor.VERDICT_OK


# ================= 多目标机（交叉编译 + qemu 执行）=================
# 这一层钉的不是「能不能跑三个目标」，而是四条判据契约：
#   1. 汇总取最差，不取多数——host 全过、ppc32 挂一条，正是换目标机要抓的
#      字节序缺陷，多数表决会把它投掉；
#   2. 工具链缺失 = 该目标未验证（blocked），绝不允许被记成通过；
#   3. 逐目标的工作目录、环境指纹、原始输出必须分开留存，出争议时能单独拿出
#      「到底哪个架构上挂的」；
#   4. 只配一个目标时，证据形状与单目标时代一字不变（向后兼容）。
def _probe_stdout(missing=()):
    """工具链探测脚本的输出：missing 里的目标报缺工具，其余报就位。"""
    lines = []
    for t in T.TARGETS:
        if t.host:
            continue                      # host 的工具链由 runner.probe 覆盖
        if t.id in missing:
            lines.append(f"WB_TARGET {t.id} missing tools= {t.cc}")
        else:
            lines.append(f"WB_TARGET {t.id} ok cc={t.cc} (Ubuntu) 7.5.0")
    return "\n".join(lines) + "\n"


_PROBE_ALL_OK = _probe_stdout()


class TargetFakeRunner(FakeRunner):
    """按远端工作目录末段（目标 id）分派 build.sh / check.sh 的输出。

    多目标时每个目标各有 <base>/<目标id> 的工作目录，而命令字面完全一样
    （都是 sh build.sh），基类按子串匹配 responses 分不出目标。
    default_tid 是必需的兜底：只配一个交叉目标时工作目录不带 /<目标id> 后缀。"""

    def __init__(self, per_target=None, default_tid="host", responses=None,
                 probe=None):
        super().__init__(responses=responses, probe=probe)
        self.per_target = dict(per_target or {})
        self.default_tid = default_tid

    def _tid_of(self, cwd) -> str:
        tail = str(cwd or "").rstrip("/").rsplit("/", 1)[-1]
        return tail if tail in self.per_target else self.default_tid

    def run(self, cmd, cwd=None, timeout=None):
        if any(key in cmd for key in self.responses):
            return super().run(cmd, cwd=cwd, timeout=timeout)
        tid = self._tid_of(cwd)
        spec = self.per_target.get(tid) or {}
        for kind, marker in (("build", "sh build.sh"), ("check", "sh check.sh")):
            if marker not in cmd:
                continue
            self.calls.append({"cmd": cmd, "cwd": cwd})   # 与基类一致：留下调用痕迹
            kw = dict(spec.get(kind) or {})
            code = int(kw.pop("exit_code", 0) or 0)
            if kind == "build":
                text = _stdout(CASE_IDS, target=tid, cwd=cwd or "", **kw)
            else:
                text = str(kw.pop("stdout", "WB_BUILD_RESULT ok\n"))
            return RunResult(cmd=cmd, cwd=cwd or "", transport=self.kind,
                             exit_code=code, stdout=text)
        return super().run(cmd, cwd=cwd, timeout=timeout)


class SyncBoomRunner(TargetFakeRunner):
    """同步到某个目标时抛异常：模拟连不上、tar 缺失、超时。"""

    fail_suffix = "/arm32"

    def sync(self, files, remote_dir):
        if str(remote_dir).endswith(self.fail_suffix):
            raise RuntimeError("tar 管道被拦截")
        return super().sync(files, remote_dir)


def _target_runner(spec, per=None, probe_missing=(), kind="build", probe=None,
                   cls=TargetFakeRunner):
    tids = T.ids(spec)
    per = per or {}
    return cls(per_target={tid: {kind: dict(per.get(tid) or {})} for tid in tids},
               default_tid=tids[0],
               responses={"#!/bin/sh": {"exit_code": 0,
                                        "stdout": _probe_stdout(probe_missing)}},
               probe=probe)


def _multi(spec, per=None, probe_missing=(), branch_min=80.0, cls=TargetFakeRunner):
    """跑一轮多目标验证，返回 (结论, 替身)。per 给每个目标定制 build.sh 输出。"""
    r = _target_runner(spec, per, probe_missing, cls=cls)
    out = executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                                    case_ids=CASE_IDS, branch_min=branch_min,
                                    targets=spec)
    return out, r


def _probe_multi(spec, checks=None, probe_missing=()):
    """跑一轮多目标编译探针，checks 给每个目标定制 check.sh 输出。"""
    r = _target_runner(spec, checks, probe_missing, kind="check")
    return executor.compile_probe(r, 1, SAMPLE.CODE_FILES, targets=spec), r


def test_multi_target_all_green_aggregates_every_target():
    out, r = _multi("host,arm32,ppc32")
    assert out["ok"] is True and out["skipped"] is False
    assert out["decision"] == executor.DECISION_NEXT
    assert out["targets"] == ["host", "arm32", "ppc32"]
    assert out["verdict"] == {"build": executor.VERDICT_OK,
                              "tests": executor.VERDICT_OK,
                              "coverage": executor.VERDICT_OK}
    assert out["tests"]["total"] == len(CASE_IDS) and out["tests"]["missing"] == 0
    assert out["coverage"]["ok"] is True
    assert out["coverage"]["merged_targets"] == ["host", "arm32", "ppc32"]
    assert out["env"]["targets"] == "host,arm32,ppc32"
    assert sum(1 for c in r.calls if c["cmd"] == "sh build.sh") == 3


def test_multi_target_workdirs_and_env_fingerprints_are_separate():
    """同一份源码同步到三个目录，各自记下当时用的是哪套编译器与选项。"""
    out, r = _multi("host,arm32,ppc32")
    base = "/fake/wb_verify/p1/v1"
    for tid in ("host", "arm32", "ppc32"):
        wd = f"{base}/{tid}"
        assert out["target_results"][tid]["workdir"] == wd
        assert out["target_results"][tid]["env"]["target"] == tid
        synced = r.files[wd]
        assert T.by_id(tid).cc in synced["build.sh"].decode("utf-8")
        assert synced["src/comm.c"], "源码没同步到 " + tid
    assert "arm32: arm-linux-gnueabihf-gcc" in out["env"]["gcc"]
    assert "ppc32: powerpc-linux-gnu-gcov" in out["env"]["gcov"]
    assert out["env"]["target_env"]["ppc32"]["qemu"] == "qemu-ppc-static"
    assert out["env"]["target_env"]["arm32"]["cflags"].endswith("-static")
    assert not out["env"]["target_env"]["host"]["cflags"].endswith("-static")


def test_multi_target_case_result_carries_per_target_status():
    """逐用例要能看出「在哪个架构上挂的」，这是归因的第一手证据。"""
    out, _ = _multi("host,arm32,ppc32", per={"arm32": {"fails": ("TC-003",)}})
    res = out["tests"]["by_id"]["TC-003"]
    assert res["status"] == parsers.ST_FAIL
    assert res["per_target"] == {"host": parsers.ST_PASS, "arm32": parsers.ST_FAIL,
                                 "ppc32": parsers.ST_PASS}
    assert "arm32" in res["detail"]
    assert out["tests"]["by_id"]["TC-001"]["status"] == parsers.ST_PASS


def test_multi_target_verdict_takes_worst_not_majority():
    """两个目标通过、一个失败 → 整体不通过。多数表决恰好会投掉字节序缺陷。"""
    out, _ = _multi("host,arm32,ppc32", per={"ppc32": {"fails": ("TC-007",)}})
    assert out["ok"] is False
    assert out["verdict"]["tests"] == executor.VERDICT_FAIL
    assert out["verdict"]["build"] == executor.VERDICT_OK
    assert out["tests"]["failed"] == 1
    assert out["tests"]["passed"] == len(CASE_IDS) - 1
    assert "1/3 个目标机未通过" in out["reason"] and "ppc32" in out["reason"]


def test_multi_target_coverage_takes_worst_not_average():
    """逐函数取最差值：平均会让一个目标上的覆盖塌陷被另外两个目标摊平。"""
    out, _ = _multi("host,arm32,ppc32", per={"arm32": {"cov": "low"}})
    cov = out["coverage"]
    assert cov["ok"] is False and "comm_pack" in cov["below_branch"]
    fn = cov["function_map"]["comm_pack"]
    assert fn["branch_effective"] == 50.0          # 不是 (100+50+100)/3
    assert fn["per_target"]["arm32"]["branch_effective"] == 50.0
    assert fn["per_target"]["host"]["branch_effective"] == 100.0
    assert out["verdict"]["coverage"] == executor.VERDICT_FAIL
    assert cov["merged_targets"] == ["host", "arm32", "ppc32"]


def test_multi_target_one_target_build_fail():
    out, _ = _multi("host,arm32", per={"arm32": {
        "build_ok": False, "exit_code": 2,
        "diags": ["src/comm.c:10:5: error: 'x' undeclared"]}})
    assert out["ok"] is False
    assert out["verdict"] == {"build": executor.VERDICT_FAIL,
                              "tests": executor.VERDICT_SKIPPED,
                              "coverage": executor.VERDICT_SKIPPED}
    # 诊断逐条标注来自哪个目标：只在某个架构上编不过时，改代码的人要知道是哪个
    errs = out["build"]["errors"]
    assert errs and {d["target"] for d in errs} == {"arm32"}
    assert "===== 目标机 arm32 =====" in out["build"]["log"]
    # 构建挂掉的目标没有覆盖率可言：合并时排除，并单独记名
    assert out["coverage"]["merged_targets"] == ["host"]
    assert "arm32：编译或链接失败" in out["reason"]


def test_multi_target_missing_toolchain_is_blocked_never_pass():
    """配了 ppc32 而验证机没装交叉工具链：结论是「该目标未验证」，转人工。"""
    out, _ = _multi("host,arm32,ppc32", probe_missing=("ppc32",))
    dead = out["target_results"]["ppc32"]
    assert dead["status"] == "unavailable" and dead["ran"] is False
    assert "powerpc-linux-gnu-gcc" in dead["reason"]
    assert out["decision"] == executor.DECISION_BLOCKED and out["ok"] is False
    assert out["target_probe"]["ppc32"]["ok"] is False
    assert out["target_probe"]["arm32"]["ok"] is True
    decision, reason = executor.auto_decision(out)
    assert decision == executor.DECISION_BLOCKED
    assert "结论不完整" in reason and "ppc32" in reason


def test_multi_target_all_cross_missing_is_environment_not_code():
    """交叉工具链一个都没装：措辞必须是「没测」而不是「编译失败」，
    否则会被当成代码缺陷去触发重生——重生也变不出编译器。"""
    out, _ = _multi("arm32,ppc32", probe_missing=("arm32", "ppc32"))
    assert out["skipped"] is True and out["ok"] is False
    assert out["decision"] == executor.DECISION_BLOCKED
    assert "工具链不可用" in out["reason"]
    assert "编译" not in out["reason"] and "失败" not in out["reason"]


def test_multi_target_sync_failure_marks_error_and_blocks():
    """同步失败的目标记 error（ran=False），整轮转人工。
    此时 host 的三维判定仍然全绿——汇总判定绿不等于这一轮可以放行。"""
    out, _ = _multi("host,arm32", cls=SyncBoomRunner)
    dead = out["target_results"]["arm32"]
    assert dead["ran"] is False and dead["status"] == "error"
    assert "同步到验证机失败" in dead["error"]
    assert dead["verdict"]["build"] == executor.VERDICT_SKIPPED
    assert out["ok"] is False
    assert out["decision"] == executor.DECISION_BLOCKED
    assert out["verdict"]["build"] == executor.VERDICT_OK
    assert "arm32" in out["reason"]


def test_multi_target_top_level_manifest_is_target_agnostic():
    """顶层输入指纹代表「源码基线」，软件工程包据此逐文件对齐；
    各目标真实同步的构建脚本指纹在 target_results 里。"""
    out, r = _multi("host,arm32,ppc32")
    assert out["manifest"] == r.manifest(
        buildkit.workspace_files(SAMPLE.CODE_FILES, SAMPLE.TEST_FILES))
    assert out["manifest"]["build.sh"]["sha256"] == \
        out["target_results"]["host"]["manifest"]["build.sh"]["sha256"]
    arm = out["target_results"]["arm32"]["manifest"]["build.sh"]["sha256"]
    assert arm != out["manifest"]["build.sh"]["sha256"]
    for tid in ("arm32", "ppc32"):
        assert out["target_results"][tid]["manifest"]["src/comm.c"] == \
            out["manifest"]["src/comm.c"], "源码指纹不应随目标变化"
    assert "manifest_note" not in out


def test_single_cross_target_keeps_single_target_evidence_shape():
    """只配一个交叉目标时证据形状不变：工作目录不加后缀、原始输出不加分隔行、
    顶层就是那个目标的结论；只有顶层指纹换成 host 变体时要说明一句。"""
    out, _ = _multi("arm32")
    base = "/fake/wb_verify/p1/v1"
    assert out["workdir"] == base
    assert out["target_results"]["arm32"]["workdir"] == base
    assert "===== 目标机" not in out["raw_log"]
    assert out["env"]["gcc"].startswith("arm-linux-gnueabihf-gcc")
    assert out["env"].get("target") == "arm32"
    assert "targets" not in out["env"]
    assert len(out["target_results"]) == 1
    assert "host" in out["manifest_note"]
    assert out["manifest"]["build.sh"] != \
        out["target_results"]["arm32"]["manifest"]["build.sh"]


def test_single_host_target_makes_no_extra_remote_call():
    """host 的工具链已由 runner.probe 覆盖：单目标路径不新增任何远端调用，
    既有项目的耗时与证据布局一字不变。"""
    r = _runner(_stdout(CASE_IDS))
    out = executor.run_verification(r, 1, 1, SAMPLE.CODE_FILES, SAMPLE.TEST_FILES,
                                    case_ids=CASE_IDS, branch_min=80.0)
    assert out["targets"] == ["host"]
    assert "target_probe" not in out
    assert "targets" not in out["env"]
    assert "manifest_note" not in out
    assert not [c for c in r.calls if c["cmd"].startswith("#!/bin/sh")]


def test_run_verification_unknown_target_is_blocked():
    out = executor.run_verification(FakeRunner(), 1, 1, SAMPLE.CODE_FILES,
                                    SAMPLE.TEST_FILES, targets="mips")
    assert out["skipped"] is True and out["ok"] is False
    assert out["decision"] == executor.DECISION_BLOCKED
    assert "目标机配置错误" in out["reason"]


# ---------- 多目标归因 ----------
def test_auto_decision_multi_cross_only_case_fail_is_portability_defect():
    """本机全过、只在交叉目标上挂用例 → 必然是可移植性缺陷，责任在被测代码。
    这条判据排在最前面：交给模型读代码反而容易判成「测试期望值写错了」。"""
    out, _ = _multi("host,arm32,ppc32", per={"ppc32": {"fails": ("TC-007",)}})
    decision, reason = executor.auto_decision(out)
    assert decision == executor.DECISION_FIX_CODE
    assert "可移植性" in reason
    assert "不得靠修改测试判据绕过" in reason
    assert "验证机本机全部通过" in reason and "ppc32" in reason


def test_auto_decision_multi_consistent_direction_across_targets():
    """所有目标都是「用例全过而覆盖率不足」→ 方向一致，直接判测试侧。"""
    out, _ = _multi("host,arm32,ppc32",
                    per={t: {"cov": "low"} for t in ("host", "arm32", "ppc32")})
    decision, reason = executor.auto_decision(out)
    assert decision == executor.DECISION_FIX_TEST
    assert "3 个目标机归因一致" in reason and "comm_pack" in reason


def test_auto_decision_multi_genuine_ambiguity_returns_none():
    """所有目标都挂同一条用例、构建与覆盖率都正常：工具链判不出方向，
    必须交给归因智能体读代码与用例。"""
    out, _ = _multi("host,arm32",
                    per={t: {"fails": ("TC-007",)} for t in ("host", "arm32")})
    assert executor.auto_decision(out) is None


def test_auto_decision_multi_mixed_directions_returns_none():
    """两个失败目标方向相反（一个缺用例、一个用例挂了）→ 不能替模型拍板。"""
    out, _ = _multi("host,arm32", per={"host": {"cov": "low"},
                                       "arm32": {"fails": ("TC-007",)}})
    assert executor.auto_decision(out) is None


def test_auto_decision_multi_all_green_returns_none():
    out, _ = _multi("host,ppc32")
    assert executor.auto_decision(out) is None


# ---------- 多目标简报与报告 ----------
def test_failure_brief_multi_lists_each_target_then_only_failed_detail():
    """归因智能体第一件要判断的事是「所有目标都挂还是只有某个架构挂」，
    所以逐目标一行结论放最前，明细只展开失败的目标，省上下文。"""
    out, _ = _multi("host,arm32,ppc32", per={"arm32": {"fails": ("TC-007",)}})
    brief = executor.failure_brief(out)
    assert "目标机逐项：" in brief
    for tid in ("host", "arm32", "ppc32"):
        assert f"- {tid}：构建 ok" in brief, tid
    assert "--- 目标机 arm32 明细 ---" in brief
    assert "--- 目标机 host 明细 ---" not in brief
    assert "[FAIL] TC-007" in brief


def test_failure_brief_single_target_has_no_target_list():
    brief = executor.failure_brief(_green_outcome(fails=("TC-003",)))
    assert "目标机逐项：" not in brief
    assert "[FAIL] TC-003" in brief


def test_markdown_report_multi_target_matrix():
    out, _ = _multi("host,arm32,ppc32", per={"ppc32": {"fails": ("TC-007",)}})
    md = executor.markdown_report(out)
    assert "## 目标机矩阵" in md
    assert "qemu-ppc-static" in md and "大端" in md
    assert "本机直接执行" in md
    assert "取各目标最差值" in md
    assert "| 用例 | 结果 | 各目标机 | 说明 |" in md
    assert "ppc32 fail" in md


def test_markdown_report_single_target_has_no_matrix():
    md = executor.markdown_report(_green_outcome())
    assert "## 目标机矩阵" not in md
    assert "各目标机" not in md
    assert "| 用例 | 结果 | 说明 |" in md


# ---------- 多目标证据归档 ----------
def _archive(out, project_id, version):
    """在临时证据目录里归档一次，返回 (相对路径, {文件名: 文本})。

    读文件必须在 TemporaryDirectory 退出之前做完：一出 with 目录就被删掉，
    把 Path 返回出去只会读到 FileNotFoundError。"""
    with tempfile.TemporaryDirectory() as tmp:
        saved = config.VERIFY_EVIDENCE_DIR
        config.VERIFY_EVIDENCE_DIR = tmp
        try:
            rel = executor.archive_evidence(out, project_id, version)
            d = Path(tmp) / f"p{project_id}" / f"v{version}"
            files = {p.name: p.read_text(encoding="utf-8")
                     for p in sorted(d.iterdir())}
        finally:
            config.VERIFY_EVIDENCE_DIR = saved
    return rel, files


def test_archive_evidence_multi_target_writes_per_target_logs():
    out, _ = _multi("host,arm32")
    out["target_results"]["host"]["raw_log"] = "HOST-RAW"
    out["target_results"]["arm32"]["raw_log"] = "ARM-RAW"
    out["raw_log"] = "JOINED-RAW"
    rel, files = _archive(out, 7, "2t2")
    assert rel.endswith("p7/v2t2/exec.log")
    assert files["exec.log"] == "JOINED-RAW"
    assert files["host.log"] == "HOST-RAW"
    assert files["arm32.log"] == "ARM-RAW"
    meta = json.loads(files["exec.json"])
    assert "raw_log" not in meta
    for tid in ("host", "arm32"):
        t = meta["target_results"][tid]
        for big in ("raw_log", "run_section", "coverage_section"):
            assert big not in t, f"{tid}.{big}"
        assert t["verdict"]["build"] == executor.VERDICT_OK


def test_archive_evidence_single_target_writes_no_per_target_log():
    """单目标时 exec.log 就是那个目标的完整原文，再写一份等于把同一批字节存两遍，
    还会让既有项目的证据目录凭空多出文件。"""
    out = _green_outcome()
    out["raw_log"] = out["target_results"]["host"]["raw_log"] = "HOST-RAW"
    _rel, files = _archive(out, 7, "1t1")
    assert sorted(files) == ["exec.json", "exec.log"]
    assert files["exec.log"] == "HOST-RAW"


def test_archive_evidence_skips_logs_of_targets_that_never_ran():
    """没跑的目标没有原始输出可存：不能凭空生成一个空 <目标id>.log 当证据。
    配了两个目标而只有一个真跑起来时，证据目录仍然退化成单目标布局。"""
    out, _ = _multi("host,ppc32", probe_missing=("ppc32",))
    _rel, files = _archive(out, 7, "3t3")
    assert sorted(files) == ["exec.json", "exec.log"]
    assert "ppc32.log" not in files
    assert files["exec.log"].strip(), "唯一跑过的目标的原文不能丢"


# ---------- 多目标编译探针 ----------
def test_compile_probe_multi_target_uses_per_target_script():
    out, r = _probe_multi("host,arm32")
    assert out["ok"] is True and out["skipped"] is False
    assert out["targets"] == ["host", "arm32"]
    assert set(out["per_target"]) == {"host", "arm32"}
    assert "===== 目标机 arm32 =====" in out["log"]
    base = f"{r.workdir(1, 'probe')}/code"
    arm = r.files[f"{base}/arm32"]["check.sh"].decode("utf-8")
    assert "arm-linux-gnueabihf-gcc" in arm and "-static" in arm
    host = r.files[f"{base}/host"]["check.sh"].decode("utf-8")
    assert "-static" not in host


def test_compile_probe_multi_target_one_fails():
    out, _ = _probe_multi("host,arm32", checks={"arm32": {
        "exit_code": 2,
        "stdout": "WB_BUILD_RESULT fail\nsrc/comm.c:3:1: error: 'x' undeclared\n"}})
    assert out["ok"] is False and out["skipped"] is False
    assert out["reason"] == "arm32：远端编译未通过"
    assert out["exit_code"] == 2
    assert "undeclared" in out["log"]


def test_compile_probe_missing_cross_toolchain_skips_that_target():
    """交叉工具链没装属于环境问题：该目标记 skipped 并点名，不触发重生，
    真正的拦截发生在执行验证节点。"""
    out, r = _probe_multi("host,ppc32", probe_missing=("ppc32",))
    assert out["ok"] is True and out["skipped"] is False
    assert out["per_target"]["ppc32"]["skipped"] is True
    assert "工具链不可用" in out["per_target"]["ppc32"]["reason"]
    assert "ppc32" in out["unavailable"]
    assert "===== 目标机 ppc32" not in out["log"]
    assert f"{r.workdir(1, 'probe')}/code/ppc32" not in r.files


def test_compile_probe_all_cross_missing_wording_is_not_build_fail():
    out, _ = _probe_multi("arm32,ppc32", probe_missing=("arm32", "ppc32"))
    assert out["ok"] is True and out["skipped"] is True
    assert out["reason"].startswith("所有目标机均未校验")
    assert "编译未通过" not in out["reason"] and "编译失败" not in out["reason"]


def test_compile_probe_unknown_target_is_error_not_degrade():
    out = executor.compile_probe(FakeRunner(), 1, SAMPLE.CODE_FILES, targets="mips")
    assert out["ok"] is False and out["skipped"] is False
    assert "目标机配置错误" in out["reason"] and out["targets"] == []


def test_compile_probe_single_target_has_no_probe_call():
    """单目标（host）探针不发工具链探测调用：既有校验路径的远端往返次数不变。"""
    r = FakeRunner(responses={"sh check.sh": {"exit_code": 0,
                                              "stdout": "WB_BUILD_RESULT ok\n"}})
    out = executor.compile_probe(r, 1, SAMPLE.CODE_FILES, targets="host")
    assert out["ok"] is True and "per_target" not in out
    assert not [c for c in r.calls if c["cmd"].startswith("#!/bin/sh")]


def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except Exception as e:
            failed.append(name)
            print("FAIL  " + name + ": " + type(e).__name__ + ": " + str(e))
    print("\n%d/%d passed" % (len(tests) - len(failed), len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
