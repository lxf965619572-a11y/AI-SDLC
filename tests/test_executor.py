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
            total_override=None, failed_override=None):
    """拼出 build.sh 的完整分段输出（env/build/run/coverage/end）。"""
    env = "\n".join([
        "uname=Linux 4.15.0-156-generic x86_64", "cores=4",
        "gcc=gcc (Ubuntu 7.5.0-3ubuntu1~18.04) 7.5.0",
        "gcov=gcov (Ubuntu 7.5.0-3ubuntu1~18.04) 7.5.0",
        "cwd=/fake/wb_verify/p1/v1",
        "cflags=" + buildkit.CFLAGS])
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
