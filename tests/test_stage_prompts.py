"""阶段提示词契约与 validator 行为测试。

用打桩的 llm_client.chat 离线驱动真实的 run_agent，因此校验的是
stage_agents 里真正生效的 validator 闭包，而不是复制品。

零第三方依赖：直接 .venv\\Scripts\\python.exe tests\\test_stage_prompts.py 即可运行；
装了 pytest 时 pytest tests/ 也能收集（函数名以 test_ 开头）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import stage_agents as sa
from agents.base_agent import AgentOutputError
from core import llm_client

_REAL_CHAT = llm_client.chat


class _Stub:
    """按调用次序返回预设响应，并记录收到的 messages。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        return self.responses.pop(0) if self.responses else ""


def _with_stub(stub, fn, *args, **kwargs):
    llm_client.chat = stub
    try:
        return fn(*args, **kwargs)
    finally:
        llm_client.chat = _REAL_CHAT


def _doc(meta_json, tail=""):
    body = "# 文档\n\n正文。\n\n```json\n" + meta_json + "\n```"
    return body + tail


# ---------- 提示词契约 ----------

def test_all_prompts_format_cleanly():
    # 大括号转义一旦写错，.format 会抛 KeyError/IndexError
    sa.REQUIREMENT_PROMPT.format(input="X", retry_block="")
    sa.HLD_PROMPT.format(input="X", retry_block="")
    sa.LLD_PROMPT.format(input="X", retry_block="")
    sa.TESTCASE_PROMPT.format(input="X", retry_block="")


def test_target_language_rule_only_on_design_stages():
    # 需求分析与实现语言无关，不应带 C/裸机约束
    assert "C 语言" not in sa.REQUIREMENT_SYSTEM
    assert "C 语言" in sa.HLD_SYSTEM
    assert "C 语言" in sa.LLD_SYSTEM


def test_retry_rules_has_no_numbering_collision():
    # 旧版 RETRY_RULES 以「4.」开头，与 TARGET_LANG_RULES 的「4.」重号
    assert not sa.RETRY_RULES.lstrip().startswith("4.")


# ---------- HLD validator：tables 可为空 ----------

HLD_OK_NO_DB = _doc('{"modules": ["comm"], "tables": [], '
                    '"apis": [{"method": "FN", "path": "comm_init", "desc": "初始化"}]}')


def test_hld_accepts_empty_tables_when_apis_present():
    md, meta = _with_stub(_Stub(HLD_OK_NO_DB), sa.design_hld, "SRS", {})
    assert meta["tables"] == []
    assert "正文" in md


def test_hld_rejects_empty_tables_and_apis():
    stub = _Stub(*[_doc('{"modules": ["comm"], "tables": [], "apis": []}')] * 3)
    try:
        _with_stub(stub, sa.design_hld, "SRS", {})
        raise AssertionError("应当校验失败")
    except AgentOutputError as e:
        assert "tables 与 apis 不可同时为空" in str(e)


def test_hld_still_requires_modules():
    stub = _Stub(*[_doc('{"modules": [], "tables": ["t"], "apis": []}')] * 3)
    try:
        _with_stub(stub, sa.design_hld, "SRS", {})
        raise AssertionError("应当校验失败")
    except AgentOutputError as e:
        assert "modules" in str(e)


def test_hld_survives_trailing_sql_block():
    # 真实故障形态：元数据块之后还有 ```sql DDL（修复前必然 meta=None）
    tail = "\n\n```sql\nCREATE TABLE t (id INT);\n```\n"
    md, meta = _with_stub(_Stub(HLD_OK_NO_DB + tail), sa.design_hld, "SRS", {})
    assert meta["modules"] == ["comm"]
    assert "```sql" in md


# ---------- 其它阶段 validator 未被改动 ----------

def test_lld_validator_unchanged():
    ok = _doc('{"data_structures": ["frame_t"], "functions": [{"name": "init"}]}')
    _, meta = _with_stub(_Stub(ok), sa.design_lld, "HLD", {}, "SRS")
    assert meta["functions"][0]["name"] == "init"

    stub = _Stub(*[_doc('{"data_structures": [], "functions": []}')] * 3)
    try:
        _with_stub(stub, sa.design_lld, "HLD", {}, "SRS")
        raise AssertionError("应当校验失败")
    except AgentOutputError:
        pass


def test_testcase_validator_requires_four_dimensions():
    ok = _doc('{"testcases": ['
              '{"id":"TC-1","type":"功能","preconditions":"p","steps":["s"],"expected":"e"},'
              '{"id":"TC-2","type":"边界","preconditions":"p","steps":["s"],"expected":"e"},'
              '{"id":"TC-3","type":"异常","preconditions":"p","steps":["s"],"expected":"e"},'
              '{"id":"TC-4","type":"场景","preconditions":"p","steps":["s"],"expected":"e"}]}')
    md, meta = _with_stub(_Stub(ok), sa.generate_testcases, "SRS", {})
    assert len(meta["testcases"]) == 4
    assert md.startswith("# 测试用例设计")


# ---------- 追溯链：软校验（两次修正机会，最后一次容忍并挂警告） ----------

STRUCTURED = ('{"objects": [{"id": "OBJ-001", "name": "1553B 总线", "source": ["D1C2"]}], '
              '"rules": [{"id": "RULE-001", "name": "超时重传三次", "source": ["D1C3"]}], '
              '"flows": []}')

SRS_META = {"functional_requirements": [
    {"id": "FR-001", "desc": "总线初始化", "priority": "P0"},
    {"id": "FR-002", "desc": "消息收发", "priority": "P0"},
    {"id": "FR-003", "desc": "上电自检", "priority": "P2"}]}


def _meta_doc(meta: dict) -> str:
    return _doc(json.dumps(meta, ensure_ascii=False))


def _fr(id_, desc, prio, src=None):
    d = {"id": id_, "desc": desc, "priority": prio}
    if src is not None:
        d["derived_from"] = src
    return d


# --- 需求分析：FR → 素材编号 ---

def test_requirement_records_source_refs():
    ok = _meta_doc({"functional_requirements": [
        _fr("FR-001", "总线初始化", "P0", ["obj-1", "RULE-001", "OBJ-999"])],
        "ambiguities": [], "risks": []})
    stub = _Stub(ok)
    _, meta = _with_stub(stub, sa.analyze_requirements, STRUCTURED)
    assert meta["functional_requirements"][0]["derived_from"] == ["OBJ-001", "RULE-001"]
    assert "_warnings" not in meta
    assert len(stub.calls) == 1


def test_requirement_source_gap_degrades_to_warning():
    bad = _meta_doc({"functional_requirements": [_fr("FR-001", "d", "P0")]})
    stub = _Stub(*[bad] * 3)
    _, meta = _with_stub(stub, sa.analyze_requirements, STRUCTURED)
    assert len(stub.calls) == 3                       # 给了两次修正机会
    assert meta["_warnings"] and "FR-001" in meta["_warnings"][0]
    assert "derived_from" in stub.calls[1][-1]["content"]   # 错误已回灌给模型


def test_requirement_invented_source_counts_as_gap():
    bad = _meta_doc({"functional_requirements": [
        _fr("FR-001", "d", "P0", ["OBJ-999"])]})
    _, meta = _with_stub(_Stub(*[bad] * 3), sa.analyze_requirements, STRUCTURED)
    assert meta["_warnings"]


def test_requirement_skips_trace_gate_without_sources():
    # 老项目的结构化数据没有编号：补算编号后仍可追责；连素材都没有时不追责
    ok = _meta_doc({"functional_requirements": [_fr("FR-001", "d", "P0")]})
    _, meta = _with_stub(_Stub(ok), sa.analyze_requirements, "[]")
    assert "_warnings" not in meta


def test_requirement_hard_validator_still_raises():
    bad = _meta_doc({"functional_requirements": []})
    try:
        _with_stub(_Stub(*[bad] * 3), sa.analyze_requirements, STRUCTURED)
        raise AssertionError("应当校验失败")
    except AgentOutputError as e:
        assert "functional_requirements" in str(e)


# --- 概要设计：模块 → FR ---

def _hld_doc(derived):
    return _meta_doc({"modules": ["comm", "app"], "tables": [],
                      "apis": [{"method": "FN", "path": "comm_init", "desc": "初始化"}],
                      "derived_from": derived})


def test_hld_normalizes_and_passes_coverage():
    stub = _Stub(_hld_doc({"comm": ["fr-1"], "app": ["FR-002", "FR-003"]}))
    _, meta = _with_stub(stub, sa.design_hld, "SRS", SRS_META)
    assert meta["derived_from"] == {"comm": ["FR-001"], "app": ["FR-002", "FR-003"]}
    assert "_warnings" not in meta
    assert len(stub.calls) == 1


def test_hld_p0_gap_degrades_to_warning():
    stub = _Stub(*[_hld_doc({"comm": ["FR-001"], "app": ["FR-003"]})] * 3)
    _, meta = _with_stub(stub, sa.design_hld, "SRS", SRS_META)
    assert len(stub.calls) == 3
    assert "FR-002" in meta["_warnings"][0]


def test_hld_module_without_fr_is_reported():
    stub = _Stub(*[_hld_doc({"comm": ["FR-001", "FR-002"]})] * 3)
    _, meta = _with_stub(stub, sa.design_hld, "SRS", SRS_META)
    assert "app" in meta["_warnings"][0]


def test_hld_drops_keys_outside_modules():
    stub = _Stub(_hld_doc({"comm": ["FR-001", "FR-002"], "app": ["FR-003"],
                           "幽灵模块": ["FR-001"]}))
    _, meta = _with_stub(stub, sa.design_hld, "SRS", SRS_META)
    assert "幽灵模块" not in meta["derived_from"]


def test_hld_drops_invented_fr_ids():
    ok = _hld_doc({"comm": ["FR-001", "FR-099"], "app": ["FR-002", "FR-003"]})
    _, meta = _with_stub(_Stub(ok), sa.design_hld, "SRS", SRS_META)
    assert meta["derived_from"]["comm"] == ["FR-001"]


def test_requirement_normalizes_fr_ids():
    ok = _meta_doc({"functional_requirements": [_fr("fr-1", "d", "P0", ["OBJ-001"])],
                    "ambiguities": [], "risks": []})
    _, meta = _with_stub(_Stub(ok), sa.analyze_requirements, STRUCTURED)
    assert meta["functional_requirements"][0]["id"] == "FR-001"


def test_hld_skips_trace_gate_without_frs():
    _, meta = _with_stub(_Stub(HLD_OK_NO_DB), sa.design_hld, "SRS", {})
    assert "_warnings" not in meta


# --- 详细设计：函数 → FR / 模块 ---

def _lld_doc(derived):
    return _meta_doc({"data_structures": ["frame_t"],
                      "functions": [{"name": "comm_init"}, {"name": "comm_send"}],
                      "derived_from": derived})


def test_lld_accepts_module_level_refs():
    stub = _Stub(_lld_doc({"comm_init": ["FR-001"], "comm_send": ["comm"]}))
    _, meta = _with_stub(stub, sa.design_lld, "HLD", {}, "SRS", SRS_META)
    assert meta["derived_from"]["comm_send"] == ["comm"]   # 间接引用原样保留
    assert "_warnings" not in meta


def test_lld_missing_ref_degrades_to_warning():
    stub = _Stub(*[_lld_doc({"comm_init": ["FR-001"]})] * 3)
    _, meta = _with_stub(stub, sa.design_lld, "HLD", {}, "SRS", SRS_META)
    assert "comm_send" in meta["_warnings"][0]


def test_lld_skips_trace_gate_without_srs_meta():
    ok = _doc('{"data_structures": ["frame_t"], "functions": [{"name": "init"}]}')
    _, meta = _with_stub(_Stub(ok), sa.design_lld, "HLD", {}, "SRS")
    assert "_warnings" not in meta


def test_lld_prompt_carries_fr_list():
    stub = _Stub(_lld_doc({"comm_init": ["FR-001"], "comm_send": ["FR-002"]}))
    _with_stub(stub, sa.design_lld, "HLD", {}, "SRS", SRS_META)
    assert "FR-003" in stub.calls[0][-1]["content"]
    assert len(stub.calls) == 1


def test_lld_drops_refs_to_unknown_modules():
    hld_meta = {"modules": ["comm"], "derived_from": {"comm": ["FR-001", "FR-002"]}}
    ok = _lld_doc({"comm_init": ["FR-001", "幽灵模块"], "comm_send": ["comm"]})
    _, meta = _with_stub(_Stub(ok), sa.design_lld, "HLD", hld_meta, "SRS", SRS_META)
    assert meta["derived_from"]["comm_init"] == ["FR-001"]
    assert meta["derived_from"]["comm_send"] == ["comm"]


# --- 测试用例：用例 → FR ---

def _tc(cases):
    return json.dumps({"testcases": cases}, ensure_ascii=False)


def _case(id_, type_, frs=None):
    c = {"id": id_, "type": type_, "preconditions": "p", "steps": ["s"], "expected": "e"}
    if frs is not None:
        c["fr_ids"] = frs
    return c


def test_testcase_records_and_normalizes_fr_ids():
    ok = _tc([_case("TC-1", "功能", ["fr-1"]), _case("TC-2", "边界", ["FR-002"]),
              _case("TC-3", "异常", ["FR-002"]), _case("TC-4", "场景", ["FR-003"])])
    stub = _Stub(ok)
    md, meta = _with_stub(stub, sa.generate_testcases, "SRS", SRS_META)
    assert meta["testcases"][0]["fr_ids"] == ["FR-001"]
    assert "_warnings" not in meta
    assert "| 验证需求 |" in md and "FR-001" in md
    assert "需求覆盖缺口" not in md
    assert "P0 需求覆盖 2/2" in md


def test_testcase_p0_gap_degrades_to_warning_and_renders_gap_table():
    bad = _tc([_case("TC-1", "功能", ["FR-001"]), _case("TC-2", "边界", ["FR-001"]),
               _case("TC-3", "异常", ["FR-001"]), _case("TC-4", "场景", ["FR-001"])])
    stub = _Stub(*[bad] * 3)
    md, meta = _with_stub(stub, sa.generate_testcases, "SRS", SRS_META)
    assert len(stub.calls) == 3
    assert "FR-002" in meta["_warnings"][0]
    assert "追溯校验未通过" in md
    assert "需求覆盖缺口" in md and "FR-002" in md and "FR-003" in md
    assert "P0 需求覆盖 1/2" in md


def test_testcase_orphan_cases_reported():
    bad = _tc([_case("TC-1", "功能"), _case("TC-2", "边界", ["FR-001"]),
               _case("TC-3", "异常", ["FR-002"]), _case("TC-4", "场景", ["FR-003"])])
    _, meta = _with_stub(_Stub(*[bad] * 3), sa.generate_testcases, "SRS", SRS_META)
    assert "TC-1" in meta["_warnings"][0]


def test_testcase_hard_validator_wins_over_soft():
    # 结构性缺陷（缺 expected）始终硬失败，不因容忍策略被放过
    bad = json.dumps({"testcases": [{"id": "TC-1", "type": "功能",
                                     "preconditions": "p", "steps": ["s"]}]})
    try:
        _with_stub(_Stub(*[bad] * 3), sa.generate_testcases, "SRS", SRS_META)
        raise AssertionError("应当校验失败")
    except AgentOutputError as e:
        assert "expected" in str(e)


def test_testcase_skips_trace_gate_without_frs():
    ok = _tc([_case("TC-1", "功能"), _case("TC-2", "边界"),
              _case("TC-3", "异常"), _case("TC-4", "场景")])
    _, meta = _with_stub(_Stub(ok), sa.generate_testcases, "SRS", {})
    assert "_warnings" not in meta


# ---------- 提示词把追溯要求写进了契约 ----------

def test_prompts_state_traceability_contract():
    assert "derived_from" in sa.REQUIREMENT_PROMPT
    assert "derived_from" in sa.HLD_PROMPT
    assert "derived_from" in sa.LLD_PROMPT
    assert "fr_ids" in sa.TESTCASE_PROMPT
    assert "P0" in sa.TESTCASE_PROMPT


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
