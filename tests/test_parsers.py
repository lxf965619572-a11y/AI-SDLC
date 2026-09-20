"""验证层解析器测试：build.sh 分段 / 用例结果行 / gcov 文本 / 覆盖率汇总。

gcov 夹具不是手写的想象输出，而是从验证机（Ubuntu 18.04，gcc/gcov 7.5.0）
实跑 `gcov -b -c -f` 抄回来的原文。解析器要当判据用，就必须对着真输出校准：
夹具一旦是凭印象写的，「解析不出来」会被误判成「覆盖率不达标」，
而这类错误在流水线里表现为一条永远修不好的假失败。

用法：.venv\\Scripts\\python.exe tests\\test_parsers.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verification import parsers


# ---------- 真实 gcov 7.5.0 输出（两个函数被调用，一个从未执行）----------
GCOV_REAL = """WB_GCOV_TARGET src/m.c
Function 'main'
Lines executed:100.00% of 3
No branches
Calls executed:100.00% of 2

Function 'm_never'
Lines executed:0.00% of 3
Branches executed:0.00% of 2
Taken at least once:0.00% of 2
No calls

Function 'm_hit'
Lines executed:100.00% of 5
Branches executed:100.00% of 4
Taken at least once:50.00% of 4
No calls

File 'src/m.c'
Lines executed:72.73% of 11
Branches executed:66.67% of 6
Taken at least once:33.33% of 6
Calls executed:100.00% of 2
Creating 'm.c.gcov'
"""

# ---------- build.sh 的分段输出（结构与 buildkit.BUILD_SH 一致）----------
BUILD_OUT = """WB_SECTION env
uname=Linux 4.15.0-156-generic x86_64
cores=4
gcc=gcc (Ubuntu 7.5.0-3ubuntu1~18.04) 7.5.0
gcov=gcov (Ubuntu 7.5.0-3ubuntu1~18.04) 7.5.0
date=2026-09-20T10:00:00Z
cwd=/home/lixf/wb_verify/p1/v1
cflags=-std=c99 -Wall -Wextra -g -O0 -fprofile-arcs -ftest-coverage
WB_SECTION build
src/comm.c:12:5: warning: unused variable 'x' [-Wunused-variable]
WB_BUILD_RESULT ok
WB_SECTION run
WB_BEGIN
TC-001 PASS
TC-002 FAIL crc 期望 0x29B1，实得 0xE5CC
WB_TOTAL 2
WB_FAILED 1
WB_END
WB_RUN_EXIT 1
WB_SECTION coverage
""" + GCOV_REAL + """WB_SECTION end
"""


# ==================== 分段输出 ====================
def test_parse_sections_splits_by_marker():
    sec = parsers.parse_sections(BUILD_OUT)
    assert set(sec) == set(parsers.KNOWN_SECTIONS)
    assert "cores=4" in sec["env"]
    assert "WB_BUILD_RESULT ok" in sec["build"]
    assert "TC-002 FAIL" in sec["run"]
    assert "Function 'm_hit'" in sec["coverage"]
    assert sec["end"] == ""
    # env 段的内容不能漏进 build 段：分段串了，判定就串了
    assert "cores=4" not in sec["build"]


def test_parse_sections_ignores_unknown_marker_and_leading_noise():
    text = "开机噪声\nWB_SECTION nope\n不该被收进来\nWB_SECTION run\nTC-001 PASS\n"
    sec = parsers.parse_sections(text)
    assert sec["run"] == "TC-001 PASS"
    assert "不该被收进来" not in "\n".join(sec.values())
    assert parsers.parse_sections("") == {k: "" for k in parsers.KNOWN_SECTIONS}
    assert parsers.parse_sections(None)["build"] == ""


def test_parse_kv_reads_env_block():
    kv = parsers.parse_kv(parsers.parse_sections(BUILD_OUT)["env"])
    assert kv["cores"] == "4"
    assert kv["uname"].startswith("Linux")
    assert kv["cflags"].startswith("-std=c99")
    assert parsers.parse_kv("# 注释=不算\n无等号行\nk = v ") == {"k": "v"}


def test_parse_diagnostics_keeps_warning_error_drops_note():
    text = ("src/comm.c:12:5: warning: unused variable 'x' [-Wunused-variable]\n"
            "tests/test_comm.c:8:1: error: 'foo' undeclared (first use here)\n"
            "src/comm.c:12:5: note: 附注不应单列\n"
            "In file included from src/comm.c:1:\n"
            "WB_BUILD_RESULT fail\n")
    got = parsers.parse_diagnostics(text)
    assert [(d["level"], d["file"], d["line"], d["col"]) for d in got] == \
        [("warning", "src/comm.c", 12, 5), ("error", "tests/test_comm.c", 8, 1)]
    assert "unused variable" in got[0]["msg"]
    assert parsers.parse_diagnostics("") == []


# ==================== 用例编号归一 ====================
def test_norm_case_id_unifies_sloppy_forms():
    for raw in ("TC-1", "tc-001", "TC_001", " TC1 ", "tc 1"):
        assert parsers.norm_case_id(raw) == "TC-001", raw
    assert parsers.norm_case_id("TC-012") == "TC-012"
    assert parsers.norm_case_id("TC-CRC_A") == "TC-CRC-A"
    assert parsers.norm_case_id(None) == ""


# ==================== 测试进程 stdout ====================
RUN_OK = """WB_BEGIN
TC-001 PASS
TC-002 PASS
TC-003 PASS
WB_TOTAL 3
WB_FAILED 0
WB_END
WB_RUN_EXIT 0
"""


def test_parse_test_output_all_pass():
    got = parsers.parse_test_output(RUN_OK, expected_ids=["TC-001", "TC-002", "TC-003"])
    assert got["all_pass"] is True
    assert got["consistent"] is True
    assert got["crashed"] is False
    assert (got["total"], got["passed"], got["failed"], got["missing"]) == (3, 3, 0, 0)
    assert got["reported_total"] == 3 and got["reported_failed"] == 0
    assert got["run_exit"] == 0
    assert got["by_id"]["TC-001"]["status"] == parsers.ST_PASS
    assert got["by_id"]["TC-001"]["status_text"] == "通过"
    assert got["expected"] == ["TC-001", "TC-002", "TC-003"]


def test_parse_test_output_records_fail_reason():
    sec = parsers.parse_sections(BUILD_OUT)["run"]
    got = parsers.parse_test_output(sec, expected_ids=["TC-001", "TC-002"])
    assert got["all_pass"] is False
    assert got["failed"] == 1 and got["passed"] == 1
    assert got["consistent"] is True            # 自报 2 条 1 失败，与解析一致
    assert got["run_exit"] == 1
    detail = got["by_id"]["TC-002"]["detail"]
    assert "0x29B1" in detail and got["by_id"]["TC-002"]["status_text"] == "失败"


def test_parse_test_output_marks_expected_but_silent_cases_missing():
    # 进程在 TC-002 之后崩了：没打出结果的用例必须显式记为 missing，不能当成通过
    got = parsers.parse_test_output("TC-001 PASS\nTC-002 PASS\nWB_RUN_EXIT 139\n",
                                    expected_ids=["TC-001", "TC-002", "TC-003"])
    assert got["missing"] == 1
    assert got["by_id"]["TC-003"]["status"] == parsers.ST_MISSING
    assert got["by_id"]["TC-003"]["status_text"] == "未执行"
    assert got["crashed"] is True               # 139 = SIGSEGV，不是正常退出
    assert got["all_pass"] is False


def test_parse_test_output_flags_inconsistent_self_report():
    # 桩自报 total=3 却只打出 2 条：「全绿」可能是假结论，必须暴露
    got = parsers.parse_test_output("TC-001 PASS\nTC-002 PASS\nWB_TOTAL 3\nWB_FAILED 0\n")
    assert got["consistent"] is False
    assert got["all_pass"] is True              # 解析到的确实都过了，但不自洽
    assert parsers.parse_test_output("TC-001 PASS\nWB_TOTAL 1\nWB_FAILED 1\n")["consistent"] is False


def test_parse_test_output_duplicate_id_keeps_last_but_counts_once():
    got = parsers.parse_test_output("TC-001 PASS\nTC-001 FAIL 第二次判定\nWB_TOTAL 1\n")
    assert got["total"] == 1
    assert got["by_id"]["TC-001"]["status"] == parsers.ST_FAIL
    assert got["consistent"] is True


def test_parse_test_output_empty_input_is_not_all_pass():
    got = parsers.parse_test_output("", expected_ids=["TC-001"])
    assert got["all_pass"] is False and got["missing"] == 1
    assert parsers.parse_test_output("")["all_pass"] is False


# ==================== gcov 文本 ====================
def test_parse_gcov_reads_real_7_5_output():
    parsed = parsers.parse_gcov(GCOV_REAL, default_file="src/m.c")
    assert sorted(parsed["functions"]) == ["m_hit", "m_never", "main"]
    assert list(parsed["files"]) == ["src/m.c"]

    hit = parsed["functions"]["m_hit"]
    assert hit.kind == "function" and hit.file == "src/m.c"
    assert hit.lines_pct == 100.0 and hit.lines_total == 5 and hit.lines_hit == 5
    # 分支覆盖取 Taken at least once：Branches executed 100% 会掩盖「只走了一个方向」
    assert hit.branch_pct == 50.0 and hit.branch_total == 4 and hit.branch_taken == 2
    assert hit.branch_eval_pct == 100.0
    assert hit.branch_effective == 50.0

    never = parsed["functions"]["m_never"]
    assert never.lines_pct == 0.0 and never.branch_pct == 0.0
    assert never.never_executed is False        # 7.5 用 0.00% 表达，不打 Never executed

    main = parsed["functions"]["main"]
    assert main.no_branches is True and main.branch_pct is None
    assert main.branch_effective == 100.0       # 无分支的函数不存在未覆盖分支
    assert main.calls_pct == 100.0 and main.calls_total == 2

    f = parsed["files"]["src/m.c"]
    assert f.kind == "file" and f.lines_total == 11 and f.lines_hit == 8
    assert f.branch_total == 6 and f.branch_taken == 2
    assert "Creating 'm.c.gcov'" not in "\n".join(f.raw)


def test_parse_gcov_accepts_count_metric_form():
    # 新版 gcov 在 -c 下把指标写成「命中数 of 总数」，解析器两种口径都要吃
    text = ("Function 'f'\n"
            "Lines executed:8 of 9\n"
            "Branches executed:4 of 4\n"
            "Taken at least once:3 of 4\n"
            "Calls executed:2 of 2\n")
    blk = parsers.parse_gcov(text)["functions"]["f"]
    assert blk.lines_hit == 8 and blk.lines_total == 9
    assert abs(blk.lines_pct - 88.89) < 0.01
    assert blk.branch_taken == 3 and blk.branch_total == 4
    assert blk.branch_effective == 75.0
    assert blk.calls_total == 2


def test_parse_gcov_handles_never_executed_and_no_branches():
    blk = parsers.parse_gcov("Function 'dead'\nNever executed\nNo branches\n")["functions"]["dead"]
    assert blk.never_executed is True
    assert blk.lines_pct == 0.0 and blk.branch_effective == 100.0


def test_parse_gcov_disambiguates_same_name_static_functions():
    text = ("Function 'check'\nLines executed:100.00% of 2\nNo branches\n"
            "Function 'check'\nLines executed:0.00% of 3\nNo branches\n")
    funcs = parsers.parse_gcov(text, default_file="src/b.c")["functions"]
    assert sorted(funcs) == ["check", "src/b.c::check"]


def test_parse_gcov_multi_groups_blocks_by_target():
    second = GCOV_REAL.replace("src/m.c", "src/n.c")
    parsed = parsers.parse_gcov_multi(GCOV_REAL + second)
    assert sorted(parsed["files"]) == ["src/m.c", "src/n.c"]
    # 两个编译单元里的同名函数按文件区分，不会互相覆盖
    assert "main" in parsed["functions"] and "src/n.c::main" in parsed["functions"]
    assert parsed["functions"]["main"].file == "src/m.c"
    assert parsed["functions"]["src/n.c::main"].file == "src/n.c"
    assert parsed["files"]["src/n.c"].lines_total == 11


def test_parse_gcov_multi_drops_headers_of_other_units():
    text = (GCOV_REAL +
            "File 'include/comm.h'\nLines executed:100.00% of 2\nNo branches\n")
    parsed = parsers.parse_gcov_multi(text)
    # 头文件被多个 .c 包含时会重复报告，只保留与本轮目标同名的 File 块
    assert sorted(parsed["files"]) == ["src/m.c"]


def test_parse_gcov_multi_without_marker_falls_back_to_single_parse():
    parsed = parsers.parse_gcov_multi(GCOV_REAL.split("WB_GCOV_TARGET src/m.c\n", 1)[1])
    assert sorted(parsed["functions"]) == ["m_hit", "m_never", "main"]
    assert list(parsed["files"]) == ["src/m.c"]
    assert parsers.parse_gcov_multi("")["functions"] == {}


def test_is_project_file_excludes_system_and_absolute_paths():
    assert parsers.is_project_file("src/comm.c")
    assert parsers.is_project_file("include/comm.h", ("src/", "include/"))
    assert not parsers.is_project_file("/usr/include/stdio.h")
    assert not parsers.is_project_file("C:/msys/usr/include/stdio.h")
    assert not parsers.is_project_file("build/src_comm.o")
    assert not parsers.is_project_file("")


# ==================== 覆盖率汇总 ====================
def test_summarize_coverage_weights_totals_and_lists_gaps():
    cov = parsers.summarize_coverage(parsers.parse_gcov_multi(GCOV_REAL),
                                     branch_min=80.0, line_min=0.0)
    tot = cov["totals"]
    assert (tot["lines_total"], tot["lines_hit"]) == (11, 8)
    assert (tot["branch_total"], tot["branch_taken"]) == (6, 2)
    assert abs(tot["line_pct"] - 72.73) < 0.01
    assert abs(tot["branch_pct"] - 33.33) < 0.01
    assert cov["ok"] is False
    assert cov["below_branch"] == ["m_hit", "m_never"]      # main 无分支，不参与判定
    assert cov["untested_functions"] == ["m_never"]
    assert cov["thresholds"] == {"branch_min": 80.0, "line_min": 0.0}
    assert cov["function_map"]["m_hit"]["branch_effective"] == 50.0
    assert [f["name"] for f in cov["files"]] == ["src/m.c"]


def test_summarize_coverage_untested_is_independent_of_threshold():
    # 门限设成 0 也不能放过「一次都没执行」的函数：那意味着它背后的需求没被验证过
    cov = parsers.summarize_coverage(parsers.parse_gcov_multi(GCOV_REAL),
                                     branch_min=0.0, line_min=0.0)
    assert cov["below_branch"] == [] and cov["below_line"] == []
    assert cov["untested_functions"] == ["m_never"]
    assert cov["ok"] is False


def test_summarize_coverage_counts_only_src_files():
    text = ("WB_GCOV_TARGET src/a.c\n"
            "Function 'fa'\nLines executed:100.00% of 4\nTaken at least once:100.00% of 4\n"
            "File 'src/a.c'\nLines executed:100.00% of 4\nTaken at least once:100.00% of 4\n"
            "WB_GCOV_TARGET tests/t.c\n"
            "Function 'ft'\nLines executed:50.00% of 4\nTaken at least once:50.00% of 4\n"
            "File 'tests/t.c'\nLines executed:50.00% of 4\nTaken at least once:50.00% of 4\n")
    cov = parsers.summarize_coverage(parsers.parse_gcov_multi(text), branch_min=80.0)
    # 测试桩自身的覆盖率不是交付判据
    assert [f["name"] for f in cov["files"]] == ["src/a.c"]
    assert cov["totals"]["lines_total"] == 4 and cov["totals"]["branch_taken"] == 4
    assert cov["below_branch"] == []          # ft 属于 tests/，不进函数级门限
    assert cov["untested_functions"] == []
    assert cov["ok"] is True
    # 函数明细仍然如实保留（证据不能被静默删掉），只是标出是否参与判定
    by_key = {d["key"]: d for d in cov["functions"]}
    assert by_key["fa"]["in_scope"] is True and by_key["ft"]["in_scope"] is False


def test_summarize_coverage_green_when_all_branches_taken():
    text = ("WB_GCOV_TARGET src/a.c\n"
            "Function 'fa'\nLines executed:100.00% of 4\nTaken at least once:100.00% of 4\n"
            "Function 'fb'\nLines executed:100.00% of 2\nNo branches\n"
            "File 'src/a.c'\nLines executed:100.00% of 6\nTaken at least once:100.00% of 4\n")
    cov = parsers.summarize_coverage(parsers.parse_gcov_multi(text), branch_min=80.0)
    assert cov["ok"] is True
    assert cov["totals"]["branch_pct"] == 100.0
    assert cov["untested_functions"] == [] and cov["below_branch"] == []


def test_summarize_coverage_below_line_threshold():
    text = ("WB_GCOV_TARGET src/a.c\n"
            "Function 'fa'\nLines executed:50.00% of 4\nTaken at least once:100.00% of 2\n"
            "File 'src/a.c'\nLines executed:50.00% of 4\nTaken at least once:100.00% of 2\n")
    cov = parsers.summarize_coverage(parsers.parse_gcov_multi(text),
                                     branch_min=80.0, line_min=90.0)
    assert cov["below_line"] == ["fa"]
    assert cov["ok"] is False
    assert abs(cov["totals"]["line_pct"] - 50.0) < 0.01


def test_summarize_coverage_empty_input_has_no_conclusion_but_is_not_ok():
    cov = parsers.summarize_coverage({"functions": {}, "files": {}}, branch_min=80.0)
    assert cov["totals"]["line_pct"] is None and cov["totals"]["branch_pct"] is None
    assert cov["functions"] == [] and cov["files"] == []
    # 没有任何覆盖率数据时 ok 为 True 是危险的，但门限判定确实无据可依：
    # 这一情形由执行器按「构建是否产出 gcda」另行判定，这里只固化当前口径
    assert cov["ok"] is True


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
