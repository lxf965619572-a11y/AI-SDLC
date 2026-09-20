"""需求追溯纯函数测试：编号规范化、覆盖计算、追溯矩阵。

零第三方依赖：直接 .venv\\Scripts\\python.exe tests\\test_trace.py 即可运行；
装了 pytest 时 pytest tests/ 也能收集（函数名以 test_ 开头）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import trace


# ---------- 编号规范化 ----------

def test_norm_fr_accepts_sloppy_forms():
    assert trace.norm_fr("FR-001") == "FR-001"
    assert trace.norm_fr("fr-1") == "FR-001"
    assert trace.norm_fr(" FR12 ") == "FR-012"
    assert trace.norm_fr("需求1") is None
    assert trace.norm_fr(None) is None


def test_norm_src_accepts_sloppy_forms():
    assert trace.norm_src("OBJ-001") == "OBJ-001"
    assert trace.norm_src("rule-2") == "RULE-002"
    assert trace.norm_src("FLOW-11") == "FLOW-011"
    assert trace.norm_src("FR-001") is None


def test_norm_refs_dedups_and_drops_invalid():
    assert trace.norm_refs(["fr-1", "FR-001", "xx", "FR-2"], "fr") == ["FR-001", "FR-002"]
    assert trace.norm_refs("FR-3", "fr") == ["FR-003"]
    assert trace.norm_refs(None, "fr") == []


def test_norm_source_tags_supports_doc_prefix():
    assert trace.norm_source_tags(["D1C3", "d2-c4", "C5", "12"]) == \
        ["D1C3", "D2C4", "C5", "C12"]
    assert trace.norm_source_tags("D1C3") == ["D1C3"]
    assert trace.norm_source_tags(None) == []


# ---------- parse 阶段编号 ----------

def test_assign_ids_numbers_all_three_kinds():
    data = {"objects": [{"name": "订单"}], "rules": [{"name": "金额校验"}, {"name": "库存"}],
            "flows": [{"name": "下单", "source": ["d1c2", "D1C2", "编造的"]}],
            "extra": 1}
    out = trace.assign_ids(data)
    assert [o["id"] for o in out["objects"]] == ["OBJ-001"]
    assert [r["id"] for r in out["rules"]] == ["RULE-001", "RULE-002"]
    assert out["flows"][0]["id"] == "FLOW-001"
    assert out["flows"][0]["source"] == ["D1C2"]      # 去重 + 丢弃非法标记
    assert out["extra"] == 1                          # 不认识的键原样保留


def test_assign_ids_is_stable_and_non_destructive():
    data = {"objects": [{"name": "A", "source": ["C1"]}], "rules": [], "flows": []}
    first = trace.assign_ids(data)
    assert trace.assign_ids(data) == first
    assert "id" not in data["objects"][0]             # 不改入参


# ---------- SRS 元数据 ----------

SRS_META = {"functional_requirements": [
    {"id": "FR-001", "desc": "初始化", "priority": "P0", "derived_from": ["obj-1"]},
    {"id": "fr-2", "desc": "收发", "priority": "p0"},
    {"id": "FR-003", "desc": "自检", "priority": "P2", "derived_from": ["RULE-002"]},
    {"id": "FR-001", "desc": "重复项", "priority": "P1"},
    {"desc": "没有编号", "priority": "P0"},
]}


def test_collect_frs_normalizes_and_dedups():
    frs = trace.collect_frs(SRS_META)
    assert [f["id"] for f in frs] == ["FR-001", "FR-002", "FR-003"]
    assert frs[0]["derived_from"] == ["OBJ-001"]
    assert frs[1]["derived_from"] == []
    assert trace.critical_ids(SRS_META) == ["FR-001", "FR-002"]


def test_collect_frs_tolerates_garbage():
    assert trace.collect_frs(None) == []
    assert trace.collect_frs({}) == []
    assert trace.collect_frs({"functional_requirements": "FR-001"}) == []


# ---------- HLD / LLD 引用映射 ----------

def test_norm_ref_map_filters_keys_and_keeps_indirect():
    raw = {" comm ": ["fr-1", "FR-002"], "未知模块": ["FR-003"], "": ["FR-004"]}
    strict = trace.norm_ref_map(raw, valid_keys={"comm"}, keep_indirect=False)
    assert strict == {"comm": ["FR-001", "FR-002"]}
    loose = trace.norm_ref_map({"order_create": ["FR-001", "订单模块"]})
    assert loose == {"order_create": ["FR-001", "订单模块"]}
    assert trace.norm_ref_map(None) == {}
    assert trace.norm_ref_map(["FR-001"]) == {}


def test_reverse_index_expands_module_names():
    hld_map = {"comm": ["FR-001"], "app": ["FR-002"]}
    assert trace.reverse_index(hld_map) == {"FR-001": ["comm"], "FR-002": ["app"]}
    # LLD 只写模块名时，按 HLD 承接关系展开回需求
    lld_map = {"comm_init": ["comm"], "app_run": ["FR-002"]}
    idx = trace.reverse_index(lld_map, hld_map)
    assert idx["FR-001"] == ["comm_init"]
    assert idx["FR-002"] == ["app_run"]


def test_uncovered_keeps_required_order():
    idx = {"FR-001": ["a"]}
    assert trace.uncovered(["FR-002", "FR-001", "FR-003"], idx) == ["FR-002", "FR-003"]
    assert trace.uncovered([], idx) == []


def test_filter_refs_drops_unknown_targets():
    refs = ["FR-001", "FR-099", "comm", "幽灵模块"]
    assert trace.filter_refs(refs, valid_frs={"FR-001", "FR-002"},
                             valid_modules={"comm"}) == ["FR-001", "comm"]
    # 无从校验时原样保留，不能把真实引用删光
    assert trace.filter_refs(refs) == refs
    assert trace.filter_refs(["FR-099"], valid_frs={"FR-001"}) == []
    assert trace.filter_refs(None) == []


def test_norm_case_refs():
    cases = trace.norm_case_refs([{"id": "TC-1", "fr_ids": ["fr-1", "xx"]},
                                  {"id": "TC-2"}, "垃圾"])
    assert cases[0]["fr_ids"] == ["FR-001"]
    assert cases[1]["fr_ids"] == []
    assert len(cases) == 2


# ---------- 追溯矩阵 ----------

def test_build_matrix_full_chain():
    hld = {"modules": ["comm", "app"],
           "derived_from": {"comm": ["FR-001"], "app": ["FR-002", "FR-003"]}}
    lld = {"functions": [{"name": "comm_init"}], "derived_from": {"comm_init": ["comm"]}}
    tc = {"testcases": [{"id": "TC-1", "fr_ids": ["FR-001", "FR-002"]},
                        {"id": "TC-2", "fr_ids": ["FR-002"]},
                        {"id": "TC-3"}]}
    m = trace.build_matrix(SRS_META, hld, lld, tc)
    rows = {r["id"]: r for r in m["rows"]}
    assert rows["FR-001"]["sources"] == ["OBJ-001"]
    assert rows["FR-001"]["hld_modules"] == ["comm"]
    assert rows["FR-001"]["lld_functions"] == ["comm_init"]   # 经模块名展开
    assert rows["FR-002"]["testcases"] == ["TC-1", "TC-2"]
    assert rows["FR-003"]["testcases"] == []

    s = m["summary"]
    assert s["fr_total"] == 3 and s["p0_total"] == 2
    assert s["uncovered_p0"]["testcase"] == []        # FR-001/002 都被 TC-1 覆盖
    assert s["uncovered_p0"]["lld"] == ["FR-002"]
    assert s["orphan_testcases"] == ["TC-3"]
    assert s["covered"]["hld"] == 2


def test_build_matrix_empty_inputs():
    m = trace.build_matrix(None, None, None, None)
    assert m["rows"] == []
    assert m["summary"]["fr_total"] == 0
    assert m["summary"]["uncovered_p0"] == {"hld": [], "lld": [], "testcase": []}


def test_build_matrix_ignores_keys_outside_modules():
    # 模型多写了 modules 之外的键，不应污染矩阵
    hld = {"modules": ["comm"], "derived_from": {"comm": ["FR-001"], "幽灵模块": ["FR-002"]}}
    m = trace.build_matrix(SRS_META, hld, None, None)
    rows = {r["id"]: r for r in m["rows"]}
    assert rows["FR-001"]["hld_modules"] == ["comm"]
    assert rows["FR-002"]["hld_modules"] == []
    assert m["summary"]["uncovered_p0"]["hld"] == ["FR-002"]


def test_recorded_links_marks_legacy_artifacts():
    # 追溯能力上线前的旧产物没有这些字段：应判为「未记录」，前端据此显示 — 而非断链
    srs = {"functional_requirements": [{"id": "FR-001", "desc": "x", "priority": "P0"}]}
    flags = trace.recorded_links(srs, {"modules": ["comm"]}, None,
                                 {"testcases": [{"id": "TC-1"}]})
    assert flags == {"source": False, "hld": False, "lld": False, "testcase": False}


def test_recorded_links_treats_empty_field_as_recorded():
    # 新产物即使值为空（模型漏标）也算「记录过」，缺口要如实报出来
    srs = {"functional_requirements": [{"id": "FR-001", "derived_from": []}]}
    flags = trace.recorded_links(srs, {"modules": ["m"], "derived_from": {}},
                                 {"derived_from": {}},
                                 {"testcases": [{"id": "TC-1", "fr_ids": []}]})
    assert flags == {"source": True, "hld": True, "lld": True, "testcase": True}


def test_build_matrix_summary_carries_recorded_flags():
    m = trace.build_matrix(SRS_META, {"modules": ["comm"]}, None, None)
    rec = m["summary"]["recorded"]
    assert rec["source"] is True          # SRS_META 里带 derived_from
    assert rec["hld"] is False            # 概设产物是旧格式
    assert rec["lld"] is False and rec["testcase"] is False


# ---------- 行级链路状态 ----------

def test_link_helpers():
    s = {"has_hld": True, "has_lld": False, "has_tc": False,
         "recorded": {"source": True, "hld": True, "lld": False, "testcase": False},
         "fr_total": 2}
    assert trace.link_produced(s, "hld") and not trace.link_produced(s, "lld")
    assert trace.link_recorded(s, "hld") and not trace.link_recorded(s, "testcase")
    assert trace.pending_links(s) == ["lld", "testcase"]
    assert trace.judgeable(s) is True
    # 没有 recorded 字段（更早的数据结构）按已记录处理，不能一律判为未记录
    assert trace.link_recorded({"fr_total": 0}, "source") is True


def test_judgeable_false_when_nothing_recorded():
    s = {"has_hld": False, "has_lld": False, "has_tc": False, "fr_total": 3,
         "recorded": {"source": False, "hld": False, "lld": False, "testcase": False}}
    assert trace.judgeable(s) is False


def test_row_status_full_chain_is_ok():
    srs = {"functional_requirements": [
        {"id": "FR-001", "desc": "x", "priority": "P0", "derived_from": ["OBJ-001"]}]}
    hld = {"modules": ["comm"], "derived_from": {"comm": ["FR-001"]}}
    lld = {"derived_from": {"comm_init": ["comm"]}}
    tc = {"testcases": [{"id": "TC-1", "fr_ids": ["FR-001"]}]}
    m = trace.build_matrix(srs, hld, lld, tc)
    row = m["rows"][0]
    assert row["missing"] == []
    assert row["status"] == trace.ST_OK and row["status_text"] == "贯通"
    assert m["summary"]["pending_links"] == []


def test_row_status_pending_when_downstream_missing():
    m = trace.build_matrix(SRS_META, None, None, None)
    assert m["summary"]["pending_links"] == ["hld", "lld", "testcase"]
    rows = {r["id"]: r for r in m["rows"]}
    # 下游都还没产出：不能报「贯通」，那是假结论
    assert rows["FR-001"]["status"] == trace.ST_PENDING
    assert rows["FR-001"]["status_text"] == "待生成：概要设计、详细设计、测试用例"
    # 但真缺口优先于待生成：FR-002 连素材来源都没标注
    assert rows["FR-002"]["status"] == trace.ST_GAP
    assert rows["FR-002"]["missing"] == ["素材来源"]


def test_row_status_unrecorded_for_legacy_artifacts():
    # 旧产物：东西在，但没有追溯字段 → 无从判断，既不报贯通也不报断链
    srs = {"functional_requirements": [{"id": "FR-001", "desc": "x", "priority": "P0"}]}
    m = trace.build_matrix(srs, {"modules": ["comm"]}, None, None)
    row = m["rows"][0]
    assert row["missing"] == []
    assert row["status"] == trace.ST_UNRECORDED
    assert row["status_text"] == "未记录"


def test_row_status_gap_beats_pending():
    # 概设已产出且记录过追溯信息，这条需求却没被覆盖：
    # 即使详设/用例还没生成，也要如实报缺口，不能被「待生成」掩盖
    srs = {"functional_requirements": [
        {"id": "FR-001", "desc": "x", "priority": "P0", "derived_from": ["OBJ-001"]}]}
    hld = {"modules": ["comm"], "derived_from": {"comm": []}}
    m = trace.build_matrix(srs, hld, None, None)
    row = m["rows"][0]
    assert row["status"] == trace.ST_GAP
    assert row["missing"] == ["概要设计"]
    assert row["status_text"] == "待补全：概要设计"


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
