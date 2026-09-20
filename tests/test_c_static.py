"""受限 C 子集静态检查测试：规则表 / 逐条判据 / 设计覆盖 / 反馈与报告渲染。

规则表（core.c_rules）是生成器提示词与检查器的单一事实来源，所以两边都要测：
  1. 表本身自洽：id 唯一、等级合法、blocking/waivable 与 severity 对得上；
  2. 每条规则都有「命中」与「不误报」两个方向的用例。只测命中会把检查器
     越写越激进——误报在流水线里表现为一条永远修不好的假失败，
     比漏报更难查，因为生成智能体每次都被要求改一段本来正确的代码。

用法：.venv\\Scripts\\python.exe tests\\test_c_static.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import c_rules, c_static
from tests.fixtures import comm_sample as SAMPLE


def check(src, opts=None, lld=None, path="src/x.c"):
    """单文件检查的快捷入口。"""
    return c_static.check_files({path: src}, opts or {}, lld)


def rules_of(report):
    return sorted(v["rule_id"] for v in report["violations"])


# ==================== 规则表自洽 ====================
def test_rule_table_ids_unique_and_well_formed():
    ids = [r.id for r in c_rules.RULES]
    assert len(ids) == len(set(ids)), "规则编号重复会让报告无法定位判据"
    for r in c_rules.RULES:
        assert r.id.startswith(("WB-C-", "WB-D-")), r.id
        assert r.severity in (c_rules.SEV_REQUIRED, c_rules.SEV_ADVISORY)
        assert r.title and r.why and r.prompt and r.standard_ref
        # 条款号待标准化部门核定，但标准名必须先落上，否则报告里的判据没有出处
        assert r.standard_ref.startswith(("GJB", "QJ")), r.standard_ref
        assert r.blocking == (r.severity == c_rules.SEV_REQUIRED)


def test_first_batch_rules_are_all_present():
    """本期冻结的判据集合：少一条就意味着某类缺陷没人管。"""
    want = {"WB-C-000", "WB-C-001", "WB-C-002", "WB-C-003", "WB-C-004",
            "WB-C-005", "WB-C-006", "WB-C-007",
            "WB-D-001", "WB-D-002", "WB-D-003"}
    assert want == {r.id for r in c_rules.RULES}


def test_safety_bottom_line_rules_are_not_waivable():
    """动态内存、递归、解析失败、设计断链不允许走偏差单放行。"""
    for rid in ("WB-C-000", "WB-C-001", "WB-C-002", "WB-D-001"):
        assert c_rules.waivable(rid) is False, rid
        assert c_rules.blocking(rid) is True, rid
    # 建议项不阻断
    assert c_rules.blocking("WB-C-005") is False
    assert c_rules.blocking("WB-D-003") is False


def test_unknown_rule_id_is_treated_conservatively():
    # 未知编号一律按「阻断且不可偏差」处理：宁可挡住人工看一眼，不可静默放行
    assert c_rules.get("WB-X-999") is None
    assert c_rules.blocking("WB-X-999") is True
    assert c_rules.waivable("WB-X-999") is False
    assert c_rules.severity_of("WB-X-999") == c_rules.SEV_REQUIRED
    assert c_rules.title_of("WB-X-999") == "WB-X-999"


def test_code_and_design_rules_partition_the_table():
    code = [r.id for r in c_rules.code_rules()]
    design = [r.id for r in c_rules.design_rules()]
    assert code == sorted(code) and design == sorted(design)
    assert set(code) | set(design) == {r.id for r in c_rules.RULES}
    assert not (set(code) & set(design))
    assert all(x.startswith("WB-C-") for x in code)
    assert all(x.startswith("WB-D-") for x in design)


def test_as_dicts_and_markdown_expose_criterion_source():
    dicts = c_rules.as_dicts()
    assert len(dicts) == len(c_rules.RULES)
    assert set(dicts[0]) == {"id", "title", "standard_ref", "severity", "waivable", "why"}
    md = c_rules.rule_table_markdown()
    assert md.startswith("| 规则编号 |")
    for r in c_rules.RULES:
        assert r.id in md and r.title in md


# ==================== 提示词与检查器同源 ====================
def test_render_injects_threshold_and_keeps_braces_literal():
    rule = c_rules.get("WB-C-004")
    out = c_rules.render(rule.prompt, 7)
    # 判据原文里的 `enum { NAME = N }` 是 C 语法示例，必须原样保留
    assert "enum { NAME = N }" in out
    assert "{complexity_max}" not in c_rules.render(c_rules.get("WB-C-006").prompt, 7)
    assert "不超过 7" in c_rules.render(c_rules.get("WB-C-006").prompt, 7)
    assert c_rules.render(None) == "" and c_rules.render("") == ""
    # 为什么不用 str.format：花括号字面量会被当成字段名，直接抛 KeyError。
    # 这条断言把「踩过一次的坑」钉住，避免有人把 render 换回 format。
    try:
        rule.prompt.format(complexity_max=7)
        raise AssertionError("str.format 应当因花括号字面量抛 KeyError")
    except KeyError:
        pass


def test_prompt_block_lists_every_code_rule_with_threshold_filled():
    block = c_rules.prompt_block(12)
    for r in c_rules.code_rules():
        assert r.id in block, r.id
        assert r.title in block, r.title
    assert "不超过 12" in block
    assert "{complexity_max}" not in block
    # 子集说明放在第一条：模型先看到「能用什么语法」，再看到逐条禁令
    assert block.splitlines()[0].startswith("- WB-C-000")
    # 设计类规则不进代码生成提示词（那是 test_impl / 设计一致性检查的事）
    assert "WB-D-001" not in block


def test_prompt_block_and_checker_agree_on_dynamic_alloc_ban():
    """提示词里写了不许的，检查器就必须抓——两边不同源就会漂移。"""
    block = c_rules.prompt_block()
    for fn in ("malloc", "calloc", "realloc", "free", "alloca"):
        assert fn in block or fn in c_rules.get("WB-C-001").prompt
        assert fn in c_rules.DYNAMIC_ALLOC_FUNCS or fn == "alloca"
    src = "#include <stdlib.h>\nint f(void)\n{\n    void *p = strdup(\"a\");\n"
    src += "    (void)p;\n    return 0;\n}\n"
    assert "WB-C-001" in rules_of(check(src))


# ==================== 合格样本：零违规 ====================
def test_sample_module_passes_with_zero_violations():
    rep = c_static.check_files(SAMPLE.CODE_FILES, {"complexity_max": 10},
                               SAMPLE.LLD_FUNCTIONS)
    assert rep["ok"] is True
    assert rep["total"] == 0 and rep["required"] == 0 and rep["advisory"] == 0
    assert rep["violations"] == [] and rep["by_rule"] == {}
    assert rep["thresholds"] == {"complexity_max": 10}
    assert [f["path"] for f in rep["files"]] == ["src/comm.c"]
    assert rep["files"][0]["parse_error"] == ""
    assert rep["files"][0]["includes"] == ["comm.h"]
    assert len(rep["rule_table"]) == len(c_rules.RULES)


def test_sample_function_facts_are_usable_as_trace_input():
    rep = c_static.check_files(SAMPLE.CODE_FILES, {}, SAMPLE.LLD_FUNCTIONS)
    by_name = {f["name"]: f for f in rep["functions"]}
    # 追溯矩阵「代码单元」列与设计覆盖检查都依赖这份函数事实
    assert set(by_name) >= {f["name"] for f in SAMPLE.LLD_FUNCTIONS}
    assert by_name["comm_crc16"]["sig"] == "uint16_t(uint8_t*,int)"
    assert by_name["comm_crc16"]["static"] is False
    # 内部辅助函数必须是 static，否则会触发 WB-D-003
    assert by_name["frame_write"]["static"] is True
    for f in rep["functions"]:
        assert f["complexity"] >= 1 and f["returns"] >= 0
        assert f["file"] == "src/comm.c" and f["line"] >= 1


def test_sample_design_signatures_match_implementation():
    """设计签名与实现签名同口径比对：不一致会造出成片的假 WB-D-002。"""
    rep = c_static.check_files(SAMPLE.CODE_FILES, {}, SAMPLE.LLD_FUNCTIONS)
    assert "WB-D-002" not in rep["by_rule"]
    by_name = {f["name"]: f for f in rep["functions"]}
    for spec in SAMPLE.LLD_FUNCTIONS:
        want = c_static.loose_sig_from_text(spec["sig"], name_hint=spec["name"])
        assert want == by_name[spec["name"]]["sig"], spec["name"]


# ==================== WB-C-001 动态内存 ====================
def test_wb_c_001_flags_each_alloc_call_with_line_and_func():
    src = ("#include <stdlib.h>\n"
           "int f(int n)\n"
           "{\n"
           "    int *p = malloc(4);\n"      # 第 4 行
           "    int *q = calloc(1, 4);\n"
           "    free(p);\n"
           "    free(q);\n"
           "    return n;\n"
           "}\n")
    rep = check(src)
    assert rep["ok"] is False
    assert rep["by_rule"]["WB-C-001"] == 4      # 一次调用一条，不按语句合并
    got = [(v["line"], v["func"]) for v in rep["violations"] if v["rule_id"] == "WB-C-001"]
    assert got == [(4, "f"), (5, "f"), (6, "f"), (7, "f")]
    assert all(v["waivable"] is False for v in rep["violations"])
    msgs = " ".join(v["message"] for v in rep["violations"])
    for fn in ("malloc", "calloc", "free"):
        assert fn in msgs, fn


def test_wb_c_001_does_not_flag_static_buffers():
    src = ("int f(int n)\n{\n    int buf[8];\n    buf[0] = n;\n    return buf[0];\n}\n")
    assert check(src)["total"] == 0


# ==================== WB-C-002 递归 ====================
def test_wb_c_002_flags_direct_recursion():
    src = "int f(int n)\n{\n    if (n <= 0) { return 0; }\n    return n + f(n - 1);\n}\n"
    rep = check(src)
    assert "WB-C-002" in rep["by_rule"]
    msg = [v["message"] for v in rep["violations"] if v["rule_id"] == "WB-C-002"][0]
    assert "f → f" in msg


def test_wb_c_002_flags_indirect_recursion():
    src = ("int b(int n);\n"
           "int a(int n)\n{\n    return b(n);\n}\n"
           "int b(int n)\n{\n    return a(n);\n}\n")
    rep = check(src)
    assert rep["by_rule"].get("WB-C-002") == 1
    msg = [v["message"] for v in rep["violations"] if v["rule_id"] == "WB-C-002"][0]
    assert "a → b → a" in msg


def test_find_recursion_reports_cycle_once_and_ignores_extern_calls():
    fns = [{"name": "a", "calls": ["b"]}, {"name": "b", "calls": ["c"]},
           {"name": "c", "calls": ["a"]}, {"name": "d", "calls": ["printf", "memcpy"]}]
    cycles = c_static.find_recursion(fns)
    assert len(cycles) == 1 and cycles[0][0] == cycles[0][-1] == "a"
    assert sorted(cycles[0]) == ["a", "a", "b", "c"]
    # 调用外部库函数不是递归：不在调用图里的名字必须被忽略
    assert c_static.find_recursion([{"name": "a", "calls": ["printf"]}]) == []
    assert c_static.find_recursion([]) == []


# ==================== WB-C-003 函数指针 ====================
def test_wb_c_003_flags_function_pointer_parameter():
    src = "int run(int (*cb)(int))\n{\n    return cb(1);\n}\n"
    rep = check(src)
    assert rep["by_rule"].get("WB-C-003") == 1
    assert rep["violations"][0]["func"] == "run"
    assert rep["violations"][0]["waivable"] is True      # 可偏差，但必须人工批


def test_wb_c_003_flags_local_and_file_scope_function_pointers():
    src = ("int (*g_hook)(int);\n"
           "int f(void)\n{\n    int (*h)(int) = 0;\n    return h ? 1 : 0;\n}\n")
    rep = check(src)
    assert rep["by_rule"].get("WB-C-003") == 2
    assert [(v["line"], v["func"]) for v in rep["violations"]] == [(1, "g_hook"), (4, "f")]
    # `int (*g_hook)(int);` 是函数指针变量，不是函数原型：措辞与定位都要对得上
    assert "函数原型" not in " ".join(v["message"] for v in rep["violations"])


def test_wb_c_003_cannot_be_bypassed_by_typedef():
    """回调 typedef 是模型最常见的写法，一层类型别名不能把判据绕过去。"""
    src = ("typedef int (*Handler)(int);\n"
           "static Handler g_hook;\n"
           "int run(Handler cb)\n{\n    Handler tab[4];\n"
           "    tab[0] = cb;\n    return g_hook ? 1 : tab[0](1);\n}\n")
    rep = check(src)
    assert rep["ok"] is False
    # typedef 本身 + 文件作用域变量 + 函数参数/局部数组，每处都要留结论
    assert rep["by_rule"].get("WB-C-003", 0) >= 4
    msgs = " ".join(v["message"] for v in rep["violations"])
    assert "typedef Handler" in msgs


def test_wb_c_003_flags_function_pointer_struct_member():
    src = ("typedef struct { int (*cb)(int); } Ctx;\n"
           "int f(Ctx *c)\n{\n    return c->cb != 0;\n}\n")
    rep = check(src)
    assert rep["ok"] is False
    assert "typedef Ctx" in " ".join(v["message"] for v in rep["violations"])


def test_wb_c_003_names_anonymous_file_scope_types():
    """没有变量名的类型定义不能报成「变量 None」：结论要能对着源码核对。"""
    src = "struct S { int (*cb)(int); };\nint f(void)\n{\n    return 0;\n}\n"
    rep = check(src)
    msg = [v["message"] for v in rep["violations"] if v["rule_id"] == "WB-C-003"][0]
    assert "None" not in msg and "结构体 S" in msg


def test_wb_c_003_does_not_flag_switch_dispatch():
    """判据推荐的替代写法（枚举 + switch）本身必须干净。"""
    src = ("int dispatch(int op, int a)\n"
           "{\n"
           "    int r = 0;\n"
           "    switch (op) {\n"
           "    case 0: r = a + 1; break;\n"
           "    case 1: r = a - 1; break;\n"
           "    default: r = 0; break;\n"
           "    }\n"
           "    return r;\n"
           "}\n")
    assert "WB-C-003" not in check(src)["by_rule"]


# ==================== WB-C-004 数组定长 ====================
def test_wb_c_004_flags_vla():
    src = "int f(int n)\n{\n    int buf[n];\n    buf[0] = 1;\n    return buf[0];\n}\n"
    rep = check(src)
    assert rep["by_rule"].get("WB-C-004") == 1
    assert "buf" in rep["violations"][0]["message"]
    assert rep["violations"][0]["line"] == 3


def test_wb_c_004_accepts_enum_macro_and_constant_expression_dims():
    src = ("enum { MAX = 32 };\n"
           "#define CAP 8\n"
           "int f(int n)\n"
           "{\n"
           "    int a[MAX];\n"
           "    int b[CAP];\n"
           "    int c[2 * MAX];\n"
           "    a[0] = n;\n    b[0] = n;\n    c[0] = n;\n"
           "    return a[0] + b[0] + c[0];\n"
           "}\n")
    assert check(src)["total"] == 0


def test_wb_c_004_accepts_unsized_array_parameter():
    """形参 buf[] 等价于指针，判据明确要求配合长度参数使用，不算 VLA。"""
    src = ("int sum(const unsigned char buf[], int len)\n"
           "{\n    int s = 0;\n    int i = 0;\n"
           "    for (i = 0; i < len; i = i + 1) { s = s + buf[i]; }\n"
           "    return s;\n}\n")
    assert "WB-C-004" not in check(src)["by_rule"]


# ==================== WB-C-005 单出口（建议项）====================
def test_wb_c_005_multi_return_is_advisory_and_does_not_block():
    src = "int f(int a)\n{\n    if (a > 0) { return 1; }\n    return 0;\n}\n"
    rep = check(src)
    assert rep["by_rule"].get("WB-C-005") == 1
    assert rep["ok"] is True                 # 建议项不阻断流水线
    assert rep["required"] == 0 and rep["advisory"] == 1
    assert rep["violations"][0]["severity"] == c_rules.SEV_ADVISORY


def test_wb_c_005_single_exit_is_clean():
    src = ("int f(int a)\n{\n    int r = 0;\n"
           "    if (a > 0) { r = 1; }\n    return r;\n}\n")
    assert check(src)["total"] == 0


# ==================== WB-C-006 圈复杂度 ====================
def test_wb_c_006_uses_threshold_from_opts():
    src = ("int f(int a, int b)\n{\n"
           "    if (a > 0 && b > 0) { return 2; }\n"
           "    return 0;\n}\n")          # 1 + if + && = 3
    assert check(src, {"complexity_max": 3})["by_rule"].get("WB-C-006") is None
    rep = check(src, {"complexity_max": 2})
    assert rep["by_rule"].get("WB-C-006") == 1
    assert "圈复杂度 3" in rep["violations"][-1]["message"] or \
           any("圈复杂度 3" in v["message"] for v in rep["violations"])
    assert rep["thresholds"] == {"complexity_max": 2}
    # 默认上限 10（未配置时不能没有上限）
    assert check(src)["thresholds"] == {"complexity_max": 10}


def test_complexity_counts_decision_points_not_statements():
    src = ("int f(int a)\n{\n    int s = 0;\n    int i = 0;\n"
           "    for (i = 0; i < 3; i = i + 1) { s = s + i; }\n"
           "    while (a > 0) { a = a - 1; }\n"
           "    s = (a > 0) ? s + 1 : s;\n"
           "    return s;\n}\n")           # 1 + for + while + ?: + if? = 见断言
    rep = check(src)
    cx = [f["complexity"] for f in rep["functions"] if f["name"] == "f"][0]
    assert cx == 4, cx                     # 1 基线 + for + while + 三元


def test_case_labels_count_but_default_does_not():
    src = ("int f(int op)\n{\n    int r = 0;\n    switch (op) {\n"
           "    case 1: r = 1; break;\n    case 2: r = 2; break;\n"
           "    default: r = 0; break;\n    }\n    return r;\n}\n")
    cx = [f["complexity"] for f in check(src)["functions"] if f["name"] == "f"][0]
    assert cx == 3, cx                     # 1 基线 + case×2；switch 与 default 不引入新路径


# ==================== WB-C-007 可写静态存储 ====================
def test_wb_c_007_flags_writable_global_and_function_static():
    src = ("int g_counter;\n"
           "static int s_state;\n"
           "int f(void)\n{\n    static int calls = 0;\n"
           "    calls = calls + 1;\n    g_counter = calls;\n    return s_state;\n}\n")
    rep = check(src)
    assert rep["by_rule"].get("WB-C-007") == 3
    assert rep["ok"] is False
    assert any(v["func"] == "f" for v in rep["violations"] if v["rule_id"] == "WB-C-007")


def test_wb_c_007_allows_const_tables_and_enum_constants():
    src = ("static const unsigned char TABLE[4] = {1, 2, 3, 4};\n"
           "static const unsigned char *const PTR = 0;\n"
           "enum { MAX = 32 };\n"
           "int f(void)\n{\n    return (int)TABLE[0] + MAX + (PTR != 0);\n}\n")
    assert check(src)["total"] == 0


def test_wb_c_007_pointer_to_const_is_still_writable_pointer():
    """`const T *p` 的指针本身可改写，判据按顶层限定词判——这是有意的严格。"""
    src = ("static const unsigned char *TABLE = 0;\n"
           "int f(void)\n{\n    return TABLE != 0;\n}\n")
    rep = check(src)
    assert rep["by_rule"].get("WB-C-007") == 1
    assert "TABLE" in rep["violations"][0]["message"]


# ==================== WB-C-000 子集与可解析性 ====================
def test_wb_c_000_flags_conditional_compilation():
    src = ("#ifdef FOO\nint f(void) { return 1; }\n#else\nint f(void) { return 0; }\n#endif\n")
    rep = check(src)
    assert rep["ok"] is False
    assert rep["by_rule"].get("WB-C-000") == 3
    lines = [v["line"] for v in rep["violations"] if v["rule_id"] == "WB-C-000"]
    assert lines == [1, 3, 5]              # 行号指回原始源码，评审才核对得了


def test_wb_c_000_flags_function_like_macro_and_pragma():
    src = ("#define SQ(x) ((x) * (x))\n#pragma once\n"
           "int f(void)\n{\n    return SQ(2);\n}\n")
    rep = check(src)
    assert rep["by_rule"].get("WB-C-000") == 2
    msgs = " ".join(v["message"] for v in rep["violations"])
    assert "SQ" in msgs and "pragma" in msgs


def test_wb_c_000_parse_failure_is_a_violation_not_a_pass():
    """解析不出结论 ≠ 合格。这是整个静态判据的地基。"""
    rep = check("@@@ this is not C @@@\n")
    assert rep["ok"] is False
    assert rep["by_rule"] == {"WB-C-000": 1}
    assert "无法按受限子集解析" in rep["violations"][0]["message"]
    assert rep["functions"] == []


def test_object_like_macros_and_includes_are_allowed():
    src = ('#include "comm.h"\n#define CAP 8\nint f(void)\n{\n    return CAP;\n}\n')
    rep = check(src)
    assert rep["total"] == 0
    assert rep["files"][0]["includes"] == ["comm.h"]


# ==================== 预处理：行号与注释 ====================
def test_preprocess_preserves_line_numbers():
    src = ('#include "a.h"\n#define N 4\n#ifdef X\nint y;\n#endif\nint buf[N];\n')
    text, includes, macros, notes = c_static.preprocess(src)
    assert text.count("\n") == src.count("\n")   # 指令原地换空行，不删行
    assert includes == ["a.h"]
    assert macros.get("N") == "4"
    assert text.splitlines()[5].strip() == "int buf[4];"
    assert [ln for ln, _ in notes] == [3, 5]


def test_preprocess_expands_macro_chains():
    src = "#define A B\n#define B 4\nint x = A;\n"
    text, _inc, macros, _notes = c_static.preprocess(src)
    # 宏表记的是定义当时的展开结果（A→B），使用处的迭代展开会一路走到字面量
    assert macros["A"] == "B" and macros["B"] == "4"
    assert text.splitlines()[2].strip() == "int x = 4;"


def test_preprocess_strips_comments_but_not_string_content():
    src = 'char *s = "a//b";\n/* 跨行\n   注释 */\nint x = 1; // 行尾注释\n'
    text, _inc, _mac, _notes = c_static.preprocess(src)
    assert '"a//b"' in text                     # 字面量里的 // 不是注释
    assert "行尾注释" not in text
    assert "跨行" not in text
    assert text.count("\n") == src.count("\n")   # 跨行注释也不能改行号


def test_header_preamble_supplies_typedefs_and_macros_to_sources():
    """pycparser 没有预处理器：头里的 typedef/宏必须由检查器自己拼进去，
    否则合格代码会因为「未知类型 CommCtx」被判成 WB-C-000。"""
    files = {
        "include/h.h": ("#ifndef H\n#define H\n#define CAP 8\n"
                        "typedef struct { int v; } Ctx;\n#endif\n"),
        "src/s.c": ('#include "h.h"\nint f(Ctx *c)\n{\n    int buf[CAP];\n'
                    "    buf[0] = c->v;\n    return buf[0];\n}\n"),
    }
    rep = c_static.check_files(files, {}, [{"name": "f", "sig": "int f(Ctx *c)"}])
    assert rep["total"] == 0, [v["message"] for v in rep["violations"]]
    assert rep["functions"][0]["sig"] == "int(Ctx*)"
    # 头文件里的 include guard 是头的正常写法，不算源文件的条件编译违规
    assert "WB-C-000" not in rep["by_rule"]


def test_line_numbers_survive_header_preamble_offset():
    """违规行号必须折算回本文件：拼了头前导之后坐标会整体偏移。"""
    files = {
        "include/h.h": "#define H 1\ntypedef int MyInt;\n",
        "src/s.c": ('#include "h.h"\n#include <stdlib.h>\n\n'
                    "int f(void)\n{\n    void *p = malloc(4);\n"
                    "    (void)p;\n    return 0;\n}\n"),
    }
    rep = c_static.check_files(files, {})
    got = [(v["rule_id"], v["line"]) for v in rep["violations"]]
    assert got == [("WB-C-001", 6)], got


# ==================== WB-D-* 设计覆盖 ====================
DESIGN_SRC = ("int calc(int a, int b)\n{\n    return a + b;\n}\n"
              "int extra_pub(int a)\n{\n    return a + 1;\n}\n"
              "static int helper(int a)\n{\n    return a;\n}\n"
              "int main(void)\n{\n    return calc(1, 2) + extra_pub(1) + helper(1);\n}\n")


def test_wb_d_001_missing_designed_function_blocks():
    rep = check(DESIGN_SRC, lld=[{"name": "calc", "sig": "int calc(int a, int b)"},
                                 {"name": "flush", "sig": "void flush(void)"}])
    assert rep["ok"] is False
    v = [x for x in rep["violations"] if x["rule_id"] == "WB-D-001"][0]
    assert v["func"] == "flush" and v["file"] == "src/x.c"
    assert v["waivable"] is False              # 设计断链不可偏差放行


def test_wb_d_002_signature_drift_is_reported_with_both_sides():
    rep = check(DESIGN_SRC, lld=[{"name": "calc", "sig": "long calc(int a, int b)"}])
    assert rep["by_rule"].get("WB-D-002") == 1
    msg = rep["violations"][0]["message"]
    assert "long(int,int)" in msg and "int(int,int)" in msg


def test_wb_d_002_ignores_cosmetic_differences():
    """const / struct / 参数名写法不同不算漂移，否则会造出成片假违规。"""
    for sig in ("int calc(int a, int b)", "int calc(int x, int y)",
                "int calc(const int a, int b)"):
        rep = check("int calc(int a, int b)\n{\n    return a + b;\n}\n", lld=[{"name": "calc", "sig": sig}])
        assert "WB-D-002" not in rep["by_rule"], sig


def test_wb_d_003_extra_public_function_is_advisory_and_main_exempt():
    rep = check(DESIGN_SRC, lld=[{"name": "calc", "sig": "int calc(int a, int b)"}])
    assert rep["ok"] is True                   # 建议项
    assert rep["by_rule"].get("WB-D-003") == 1
    v = [x for x in rep["violations"] if x["rule_id"] == "WB-D-003"][0]
    assert v["func"] == "extra_pub"            # static 辅助函数与 main 都不计


def test_design_coverage_is_skipped_without_design_list():
    rep = check(DESIGN_SRC, lld=None)
    assert rep["total"] == 0
    rep2 = c_static.check_files(SAMPLE.CODE_FILES, {}, [])
    assert "WB-D-001" not in rep2["by_rule"]
    assert c_static.design_coverage([], []) == []


# ==================== 签名归一化 ====================
def test_loose_sig_from_text_normal_forms():
    assert c_static.loose_sig_from_text("int calc(int a, int b)") == "int(int,int)"
    assert c_static.loose_sig_from_text("void comm_init(CommCtx *ctx)") == "void(CommCtx*)"
    assert c_static.loose_sig_from_text(
        "uint16_t comm_crc16(const uint8_t *data, int len)") == "uint16_t(uint8_t*,int)"
    assert c_static.loose_sig_from_text("int f(void)") == "int()"
    assert c_static.loose_sig_from_text("int f();") == "int()"
    assert c_static.loose_sig_from_text(
        "int comm_pack(CommCtx *ctx, const uint8_t *payload, int len,"
        " uint8_t *out, int out_cap)") == "int(CommCtx*,uint8_t*,int,uint8_t*,int)"


def test_loose_sig_from_text_returns_none_on_junk():
    for bad in ("", None, "   ", "垃圾文本没有括号", "int calc"):
        assert c_static.loose_sig_from_text(bad) is None, bad


def test_loose_sig_defaults_to_int_when_return_type_omitted():
    # K&R 风格散文常省返回类型，默认按 int 处理，与 C 的隐式 int 一致
    assert c_static.loose_sig_from_text("calc(int, int)") == "int(int,int)"


def test_loose_sig_from_ast_matches_text_form():
    facts = c_static.parse_file("src/x.c", "int calc(int a, int b)\n{\n    return a + b;\n}\n")
    from pycparser import c_ast
    decl = [n for n in facts.ast.ext if isinstance(n, c_ast.FuncDef)][0].decl
    assert c_static.loose_sig_from_ast(decl) == "int(int,int)"
    # 传 Decl 或直接传 FuncDecl 都要给出同一结论
    assert c_static.loose_sig_from_ast(decl.type) == "int(int,int)"


# ==================== 反馈与报告 ====================
def test_feedback_text_is_empty_when_no_required_violation():
    clean = c_static.check_files(SAMPLE.CODE_FILES, {}, SAMPLE.LLD_FUNCTIONS)
    assert c_static.feedback_text(clean) == ""
    advisory_only = check("int f(int a)\n{\n    if (a) { return 1; }\n    return 0;\n}\n")
    assert advisory_only["required"] == 0
    assert c_static.feedback_text(advisory_only) == ""


def test_feedback_text_carries_rule_id_location_and_criterion():
    rep = check("#include <stdlib.h>\nint f(void)\n{\n    free(0);\n    return 0;\n}\n")
    text = c_static.feedback_text(rep)
    assert "WB-C-001" in text and "禁止动态内存分配" in text
    assert "src/x.c:4" in text
    # 回灌必须带判据原文，否则模型只能猜
    assert "禁止调用 malloc" in text
    assert "必查项违规" in text


def test_feedback_text_respects_limit_and_reports_remainder():
    src = ("#include <stdlib.h>\nint f(void)\n{\n"
           "    free(0);\n    free(0);\n    free(0);\n    return 0;\n}\n")
    rep = check(src)
    assert rep["by_rule"]["WB-C-001"] == 3
    text = c_static.feedback_text(rep, limit=1)
    assert text.count("WB-C-001") == 1          # 只保留 limit 条明细
    assert "另有 2 处" in text
    assert "另有" not in c_static.feedback_text(rep, limit=10)
    assert c_static.feedback_text(rep, limit=10).count("WB-C-001") == 3


def test_markdown_report_states_verdict_and_criterion_source():
    rep = check("int f(int a)\n{\n    if (a) { return 1; }\n    return 0;\n}\n")
    md = c_static.markdown_report(rep)
    assert md.startswith("# 静态检查报告")
    assert "通过（无必查项违规）" in md          # 只有建议项时结论仍是通过
    assert "WB-C-005" in md and "函数清单" in md
    assert "判据来源" in md and "GJB 8114" in md
    assert c_static.markdown_report(rep, title="代码静态检查") .startswith("# 代码静态检查")


def test_markdown_report_marks_failure_and_lists_details():
    rep = check("@@@ not C @@@\n")
    md = c_static.markdown_report(rep)
    assert "不通过（存在必查项违规）" in md
    assert "必查项 1 条" in md
    assert "违规明细" in md
    # 明细里的竖线要转义，否则 Markdown 表格会被撑破
    assert "| WB-C-000 |" in md


def test_report_shape_is_stable_for_artifact_storage():
    rep = check("int g;\nint f(void)\n{\n    return g;\n}\n")
    assert set(rep) == {"ok", "total", "required", "advisory", "waivable_required",
                        "non_waivable_required", "by_rule", "violations", "functions",
                        "files", "thresholds", "rule_table"}
    v = rep["violations"][0]
    assert set(v) == {"rule_id", "title", "file", "line", "func", "message",
                      "severity", "waivable", "standard_ref"}
    # 违规按「阻断优先 → 规则号 → 文件 → 行」排序：报告可读，且逐轮可对比
    assert rep["violations"] == sorted(rep["violations"],
                                       key=lambda x: (0 if x["severity"] == "required" else 1,
                                                      x["rule_id"], x["file"], x["line"]))
    assert rep["required"] == 1 and rep["waivable_required"] == 1
    assert rep["non_waivable_required"] == 0


def test_split_files_separates_sources_from_resources():
    sources, others = c_static.split_files(
        {"src/a.c": "1", "include/a.h": "2", "tests/t.c": "3", "README.md": "4"})
    assert sorted(sources) == ["src/a.c", "tests/t.c"]
    assert sorted(others) == ["README.md", "include/a.h"]
    assert c_static.split_files(None) == ({}, {})
    assert c_static.split_files({"src\\win.c": "x"})[0] == {"src/win.c": "x"}


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
