"""buildkit（构建脚本 / 编译探针 / 测试桩 / 工作区拼装）的测试。

第一性原理：buildkit 是「执行环境可复现」的地基——同一份输入必须在验证机上走
同一条命令序列，证据才可比对、可复算。这里钉三类契约：
  1. 路径规范化是安全边界：模型给出的 `..` / 绝对路径 / 反斜杠 / `./` 必须在
     同步前被挡掉或归一，否则 tar 解包会落到工作区外面（路径穿越）；
  2. 测试桩权威：结果行格式是解析器的契约，生成物不得覆盖 tests/wb_harness.*，
     否则「全绿」不再可信；
  3. 脚本模板自洽：占位符必须全部替换干净，link 块只在链接探针里出现，
     编译探针不带覆盖率插桩（只判能不能编），完整脚本带覆盖率与分段标记。

零第三方依赖、可离线运行：
    .venv\\Scripts\\python.exe tests\\test_buildkit.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verification import buildkit as bk


# ==================== 退出码约定 ====================
def test_exit_code_constants_are_frozen():
    """退出码是 build.sh / check.sh 与解析器之间的硬约定，改一个就全链路错位。"""
    assert (bk.EXIT_OK, bk.EXIT_BUILD_FAIL, bk.EXIT_NO_SOURCE) == (0, 2, 3)


# ==================== 路径规范化：安全边界 ====================
def test_normalize_path_accepts_convention_relative_paths():
    assert bk.normalize_path("src/order.c") == "src/order.c"
    assert bk.normalize_path("include/order.h") == "include/order.h"
    assert bk.normalize_path("tests/test_order.c") == "tests/test_order.c"


def test_normalize_path_collapses_dot_slash_and_duplicate_separators():
    assert bk.normalize_path("./src/a.c") == "src/a.c"
    assert bk.normalize_path("src/./a.c") == "src/a.c"
    assert bk.normalize_path("src//a.c") == "src/a.c"
    assert bk.normalize_path("  include/a.h  ") == "include/a.h"


def test_normalize_path_converts_backslash_to_forward_slash():
    # 模型在 Windows 语境下常写反斜杠；同步用 tar，必须是正斜杠
    assert bk.normalize_path("src\\order.c") == "src/order.c"
    assert bk.normalize_path("tests\\sub\\t.c") == "tests/sub/t.c"


def test_normalize_path_rejects_traversal_and_absolute():
    """路径穿越守卫：越界一律返回空串，交给调用方丢弃。"""
    assert bk.normalize_path("../escape.c") == ""
    assert bk.normalize_path("src/../../etc/passwd") == ""
    assert bk.normalize_path("/abs/a.c") == ""
    assert bk.normalize_path("C:/abs/a.c") == ""   # Windows 盘符绝对路径同样拒绝
    assert bk.normalize_path("C:\\abs\\a.c") == ""  # 反斜杠归一后仍是盘符绝对路径


def test_normalize_path_rejects_empty_and_bare_dot():
    assert bk.normalize_path("") == ""
    assert bk.normalize_path(None) == ""
    assert bk.normalize_path(".") == ""
    assert bk.normalize_path("./") == ""


# ==================== 工作区拼装 ====================
def test_workspace_files_includes_build_script_and_normalizes_paths():
    ws = bk.workspace_files({"src/a.c": "A", "include/a.h": "H"},
                            {"tests/t.c": "int main(){return 0;}"})
    assert ws["build.sh"] == bk.build_script()
    assert ws["src/a.c"] == "A" and ws["include/a.h"] == "H"
    assert ws["tests/t.c"] == "int main(){return 0;}"


def test_workspace_files_drops_traversal_paths_before_sync():
    ws = bk.workspace_files({"../evil.c": "X", "src/ok.c": "O"}, None)
    assert "../evil.c" not in ws and "evil.c" not in ws
    assert ws["src/ok.c"] == "O"


def test_workspace_files_handles_none_inputs():
    ws = bk.workspace_files(None, None)
    # 至少要含构建脚本与测试桩，否则验证机无从编译
    assert ws["build.sh"] == bk.build_script()
    assert set(bk.HARNESS_FILES) <= set(ws)


def test_workspace_files_harness_is_authoritative_over_generated():
    """测试桩是结果行契约：生成物即便同名也必须被权威桩覆盖，否则全绿不可信。"""
    ws = bk.workspace_files(
        {"src/a.c": "A"},
        {"tests/wb_harness.c": "GENERATED JUNK", "tests/wb_harness.h": "JUNK"})
    assert ws["tests/wb_harness.c"] == bk.HARNESS_C
    assert ws["tests/wb_harness.h"] == bk.HARNESS_H
    assert "GENERATED JUNK" not in ws["tests/wb_harness.c"]


def test_harness_files_are_the_two_contract_files():
    assert set(bk.HARNESS_FILES) == {"tests/wb_harness.c", "tests/wb_harness.h"}
    # 桩必须自报结果行，且汇总与解析器交叉校验
    assert "wb_report" in bk.HARNESS_C and "WB_TOTAL" in bk.HARNESS_C
    assert "WB_FAILED" in bk.HARNESS_C and "WB_BEGIN" in bk.HARNESS_C
    assert "WB_CHECK" in bk.HARNESS_H


# ==================== 角色划分 ====================
def test_split_by_role_buckets_by_convention():
    files = {"src/a.c": "A", "include/a.h": "H", "tests/t.c": "T", "readme.md": "R"}
    roles = bk.split_by_role(files)
    assert list(roles["src"]) == ["src/a.c"]
    assert list(roles["include"]) == ["include/a.h"]
    assert list(roles["tests"]) == ["tests/t.c"]
    assert list(roles["other"]) == ["readme.md"]


def test_split_by_role_rejects_wrong_extension_in_src_and_include():
    """src/ 只认 .c，include/ 只认 .h；放错位置归 other（不会被 build.sh 编译）。"""
    roles = bk.split_by_role({"src/x.h": "XH", "include/y.c": "YC"})
    assert roles["src"] == {} and roles["include"] == {}
    assert sorted(roles["other"]) == ["include/y.c", "src/x.h"]


def test_split_by_role_skips_illegal_paths():
    roles = bk.split_by_role({"../bad.c": "B", "src/ok.c": "O"})
    assert list(roles["src"]) == ["src/ok.c"]
    assert all("../bad.c" not in b for b in roles.values())


# ==================== main 探测 ====================
def test_has_main_detects_int_and_void_main():
    assert bk.has_main({"tests/t.c": "int main(void){return 0;}"}) is True
    assert bk.has_main({"tests/t.c": "void main(){}"}) is True


def test_has_main_false_when_absent_or_empty():
    assert bk.has_main({"tests/t.c": "int foo(){return 0;}"}) is False
    assert bk.has_main({}) is False
    assert bk.has_main(None) is False


# ==================== 目录约定反馈 ====================
def test_layout_feedback_empty_when_layout_valid():
    fb = bk.layout_feedback({"src/a.c": "A", "include/a.h": "H"},
                            {"tests/t.c": "int main(){return 0;}"})
    assert fb == ""


def test_layout_feedback_reports_each_problem_as_dash_line():
    fb = bk.layout_feedback({"src/a.h": "H", "../x": "B"},
                            {"other/t.c": "no main here"})
    lines = [ln for ln in fb.splitlines() if ln.strip()]
    assert lines and all(ln.startswith("- ") for ln in lines)
    joined = fb
    assert "不在约定目录" in joined            # src/a.h 放错目录
    assert "非法或越出工作区" in joined         # ../x 越界
    assert "src/ 下没有任何 .c" in joined       # 没有可编译实现
    assert "测试代码 other/t.c 不在约定目录" in joined
    assert "没有定义 main" in joined            # 测试无 main 无法链接


def test_layout_feedback_only_checks_tests_when_provided():
    # test_files=None 时不应就测试目录发表意见
    fb = bk.layout_feedback({"src/a.c": "A"}, None)
    assert fb == ""


# ==================== 编译探针脚本 ====================
def test_check_script_substitutes_all_placeholders():
    for link in (False, True):
        cs = bk.check_script(link=link)
        assert "__CFLAGS_NO_COV__" not in cs
        assert "__INCLUDES__" not in cs
        assert "__LINK_BLOCK__" not in cs
        assert bk.CFLAGS_NO_COV in cs and bk.INCLUDES in cs


def test_check_script_has_no_coverage_instrumentation():
    """探针只判「能不能编/能不能链」，插桩会引入额外告警面，必须不带覆盖率。"""
    for link in (False, True):
        cs = bk.check_script(link=link)
        assert "-fprofile-arcs" not in cs
        assert "-ftest-coverage" not in cs


def test_check_script_link_block_only_when_linking():
    nolink = bk.check_script(link=False)
    withlink = bk.check_script(link=True)
    assert "-o build/wb_tests" not in nolink
    assert "link_fail" not in nolink
    assert "-o build/wb_tests" in withlink
    assert "link_fail" in withlink


def test_check_script_keeps_exit_code_contract():
    cs = bk.check_script(link=True)
    assert "exit 3" in cs and "exit 2" in cs and "exit 0" in cs
    assert "WB_BUILD_RESULT" in cs


# ==================== 完整构建脚本 ====================
def test_build_script_substitutes_flags_and_includes():
    bs = bk.build_script()
    assert "__CFLAGS__" not in bs and "__INCLUDES__" not in bs
    assert bk.CFLAGS in bs and bk.INCLUDES in bs


def test_build_script_carries_coverage_flags():
    bs = bk.build_script()
    assert "-fprofile-arcs" in bs and "-ftest-coverage" in bs
    assert "gcov -b -c -f" in bs


def test_build_script_has_all_section_markers():
    """分段标记是解析器切分 env/build/run/coverage 的依据，缺一即解析错位。"""
    bs = bk.build_script()
    for mark in ("sec env", "sec build", "sec run", "sec coverage", "sec end",
                 "WB_SECTION", "WB_BUILD_RESULT", "WB_RUN_EXIT", "WB_GCOV_TARGET"):
        assert mark in bs, mark


def test_build_script_links_test_binary_and_only_instruments_src():
    bs = bk.build_script()
    assert "-o build/wb_tests" in bs
    # 覆盖率只对 src/ 采集：测试桩自身的覆盖率不是交付判据
    assert "for f in src/*.c" in bs


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
