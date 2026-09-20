"""追溯矩阵「代码验证闭环」四列的测试：代码单元 / 静态检查 / 执行结果 / 分支覆盖。

第一性原理：这四列是把「工具链的确定性结论」折回需求维度的唯一通道。
它们错了，交付件里的追溯矩阵就会给出与实测相反的结论——比断链更危险，
因为断链一眼能看出来，而「明明用例挂了却显示贯通」看不出来。
所以这里重点钉三件事：
  1. 未产出（None）与「产出了但这条需求是空的」必须区分开，老项目不能报红；
  2. 执行失败 > 覆盖不足 > 文档链路四态，优先级不许漂；
  3. 分支覆盖取该需求名下所有函数的最差值（木桶口径，不是平均）。

夹具刻意让 FR-001 / FR-002 各挂两条落地用例：只有一条时「部分失败」与
「全部失败」无法区分，三态判定就测不到点上。

零第三方依赖：直接 .venv\\Scripts\\python.exe tests\\test_trace_verify.py 即可运行。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import trace
from tests.fixtures import comm_sample as SAMPLE


# ---------- 夹具：四份文档产物 + 四份验证产物 ----------

SRS = {"functional_requirements": [
    {"id": "FR-001", "desc": "成帧并登记重传窗口", "priority": "P0",
     "derived_from": ["OBJ-001"]},
    {"id": "FR-002", "desc": "CRC-16 校验", "priority": "P0",
     "derived_from": ["RULE-001"]},
    {"id": "FR-003", "desc": "解帧与序号校验", "priority": "P1",
     "derived_from": ["OBJ-002"]},
    {"id": "FR-004", "desc": "确认释放窗口", "priority": "P1",
     "derived_from": ["FLOW-001"]},
]}

HLD = {"modules": ["comm"],
       "derived_from": {"comm": ["FR-001", "FR-002", "FR-003", "FR-004"]}}

_FN_OF_FR = {"comm_pack": ["FR-001"], "comm_crc16": ["FR-002"],
             "comm_unpack": ["FR-003"], "comm_ack": ["FR-004"]}

LLD = {"functions": [{"name": n} for n in _FN_OF_FR],
       "derived_from": {k: list(v) for k, v in _FN_OF_FR.items()}}

# 落地用例 → 需求编号（test_impl 阶段登记的 cases 就按这个填）
CASES = {"TC-001": ["FR-002"], "TC-002": ["FR-002"],
         "TC-003": ["FR-001"], "TC-004": ["FR-001"],
         "TC-007": ["FR-003"]}

CASE_FN = {"TC-001": "comm_crc16", "TC-002": "comm_crc16",
           "TC-003": "comm_pack", "TC-004": "comm_pack",
           "TC-007": "comm_unpack"}

# 设计用例 = 落地的 5 条 + TC-011（FR-004 的用例故意不落地）
TC = {"testcases": [{"id": cid, "fr_ids": list(refs)} for cid, refs in CASES.items()]
      + [{"id": "TC-011", "fr_ids": ["FR-004"]}]}

CODE = {"files": ["include/comm.h", "src/comm.c"],
        "derived_from": {k: list(v) for k, v in _FN_OF_FR.items()}}

STATIC = {"ok": True, "required": 0, "advisory": 0,
          "functions": [{"name": n, "file": "src/comm.c"} for n in _FN_OF_FR],
          "violations": []}

TEST_IMPL = {"files": ["tests/test_comm.c"],
             "cases": [{"id": cid, "fn": CASE_FN[cid], "fr_ids": list(refs)}
                       for cid, refs in CASES.items()]}

BRANCH = {"comm_crc16": 100.0, "comm_pack": 95.0,
          "comm_unpack": 90.0, "comm_ack": 100.0}


def make_exec(fail=(), missing=(), branch=None, extra=None):
    """拼一份 exec 产物元数据（只保留矩阵用得上的两个字段）。

    默认全绿。fail 注入「判为失败」，missing 注入「进程没打出结果」，
    extra 是 test_impl 没登记的多余用例（用来验证它们不参与归属）。
    """
    statuses = {cid: ("fail" if cid in fail else
                      "missing" if cid in missing else "pass") for cid in CASES}
    statuses.update(extra or {})
    fmap = {fn: {"name": fn, "file": "src/comm.c", "in_scope": True,
                 "branch_effective": pct}
            for fn, pct in (BRANCH if branch is None else branch).items()}
    return {"tests": {"by_id": {cid: {"id": cid, "status": st}
                                for cid, st in statuses.items()}},
            "coverage": {"function_map": fmap}}


ALL_PASS = make_exec()


def build(code=CODE, static=STATIC, test_impl=TEST_IMPL, exec_meta=ALL_PASS,
          coverage_min=80.0, srs=SRS, lld=LLD):
    return trace.build_matrix(srs, HLD, lld, TC, code_meta=code,
                              static_meta=static, test_impl_meta=test_impl,
                              exec_meta=exec_meta, coverage_min=coverage_min)


def rows_of(matrix):
    return {r["id"]: r for r in matrix["rows"]}


# ---------- 未产出语义：None 与空值必须分开 ----------

def test_new_columns_are_none_when_artifacts_missing():
    """老项目（只有四份文档产物）：四列一律 None，展示为「未产出」。"""
    rows = rows_of(trace.build_matrix(SRS, HLD, LLD, TC))
    for row in rows.values():
        assert row["code_units"] is None
        assert row["static_violations"] is None
        assert row["exec_result"] is None
        assert row["branch_coverage"] is None


def test_missing_artifacts_do_not_change_legacy_status():
    """新增四列缺席时，行状态必须与闭环上线前完全一致（不许被新维度污染）。"""
    m = trace.build_matrix(SRS, HLD, LLD, TC)
    s = m["summary"]
    for row in m["rows"]:
        assert row["status"] == trace.status_of(row["missing"], s)
        assert row["status"] in (trace.ST_OK, trace.ST_GAP,
                                 trace.ST_UNRECORDED, trace.ST_PENDING)
    assert rows_of(m)["FR-001"]["status"] == trace.ST_OK


def test_code_units_empty_list_means_produced_but_uncovered():
    """代码产物在、但这条需求没有对应函数：空列表（真缺口），不是 None。"""
    code = {"files": ["src/comm.c"], "derived_from": {"comm_crc16": ["FR-002"]}}
    rows = rows_of(build(code=code))
    assert rows["FR-002"]["code_units"] == ["comm_crc16"]
    assert rows["FR-001"]["code_units"] == []
    assert rows["FR-001"]["code_units"] is not None


def test_static_violations_none_without_static_artifact():
    rows = rows_of(build(static=None))
    assert all(r["static_violations"] is None for r in rows.values())


def test_exec_result_none_without_test_impl_artifact():
    """执行结论按「落地的用例」归属需求，没有 test_impl 就无从归属。"""
    rows = rows_of(build(test_impl=None))
    assert all(r["exec_result"] is None for r in rows.values())


def test_branch_coverage_none_without_exec_artifact():
    rows = rows_of(build(exec_meta=None))
    assert all(r["branch_coverage"] is None for r in rows.values())
    assert all(r["exec_result"] == "not_run" for r in rows.values())


# ---------- 代码单元列 ----------

def test_code_units_come_from_code_meta_not_lld():
    """代码列以代码产物为准：详设写了、代码没实现，这一列就该是空的。"""
    code = {"files": ["src/comm.c"],
            "derived_from": {"comm_pack": ["FR-001"], "comm_crc16": ["FR-002"]}}
    rows = rows_of(build(code=code))
    assert rows["FR-001"]["code_units"] == ["comm_pack"]
    assert rows["FR-002"]["code_units"] == ["comm_crc16"]
    assert rows["FR-003"]["code_units"] == []
    # 详设列不受影响，两列并排看才暴露「设计有、代码无」
    assert rows["FR-003"]["lld_functions"] == ["comm_unpack"]


def test_code_units_normalizes_sloppy_fr_ids():
    code = {"derived_from": {"comm_ack": ["fr-4", "FR-004", "FR-099", "垃圾"]}}
    rows = rows_of(build(code=code))
    assert rows["FR-004"]["code_units"] == ["comm_ack"]
    assert "FR-099" not in rows


# ---------- 静态检查列 ----------

def test_static_violations_counted_per_row_function():
    static = {"ok": False, "required": 3, "advisory": 0,
              "functions": [{"name": n} for n in _FN_OF_FR],
              "violations": [{"rule_id": "WB-C-005", "func": "comm_pack"},
                             {"rule_id": "WB-C-005", "func": "comm_pack"},
                             {"rule_id": "WB-C-001", "func": "comm_crc16"}]}
    rows = rows_of(build(static=static))
    assert rows["FR-001"]["static_violations"] == 2
    assert rows["FR-002"]["static_violations"] == 1
    assert rows["FR-003"]["static_violations"] == 0


def test_static_violations_fall_back_to_lld_functions():
    """没有代码产物时按详设函数归因，静态列依然有结论（不留白）。"""
    static = {"functions": [{"name": "comm_unpack"}],
              "violations": [{"rule_id": "WB-D-001", "func": "comm_unpack"}]}
    rows = rows_of(build(code=None, static=static))
    assert rows["FR-003"]["static_violations"] == 1
    assert rows["FR-003"]["code_units"] is None


def test_static_file_level_violations_are_not_attributed_to_rows():
    """文件级违规挂不到具体需求：只在概览里计数，不许摊到某一行头上。"""
    static = {"functions": [{"name": "comm_pack"}],
              "violations": [{"rule_id": "WB-C-007", "func": ""},
                             {"rule_id": "WB-C-007"}]}
    m = build(static=static)
    assert m["summary"]["static_unattributed"] == 2
    assert rows_of(m)["FR-001"]["static_violations"] == 0


def test_static_summary_counts_functions():
    m = build()
    assert m["summary"]["static_functions"] == len(_FN_OF_FR)
    assert m["summary"]["static_unattributed"] == 0


# ---------- 执行结果列 ----------

def test_exec_result_all_when_every_landed_case_passes():
    rows = rows_of(build())
    assert rows["FR-001"]["exec_result"] == "all"
    assert rows["FR-002"]["exec_result"] == "all"
    assert rows["FR-003"]["exec_result"] == "all"


def test_exec_result_partial_when_some_cases_fail():
    rows = rows_of(build(exec_meta=make_exec(fail=("TC-003",))))
    assert rows["FR-001"]["exec_result"] == "partial"
    assert rows["FR-002"]["exec_result"] == "all"


def test_exec_result_none_when_all_cases_fail():
    rows = rows_of(build(exec_meta=make_exec(fail=("TC-001", "TC-002"))))
    assert rows["FR-002"]["exec_result"] == "none"
    rows = rows_of(build(exec_meta=make_exec(fail=("TC-003", "TC-004"))))
    assert rows["FR-001"]["exec_result"] == "none"


def test_exec_result_not_run_without_exec_artifact():
    rows = rows_of(build(exec_meta=None))
    assert rows["FR-001"]["exec_result"] == "not_run"
    assert rows["FR-004"]["exec_result"] == "not_run"


def test_exec_result_no_case_when_design_case_never_landed():
    """TC-011 在设计里有、在 test_impl 里没落地：FR-004 判「无关联用例」。

    这一态必须存在，否则「用例根本没写」会被算成「全部通过」（空集合上的
    all() 为真），是最典型的一种假绿。"""
    rows = rows_of(build())
    assert rows["FR-004"]["exec_result"] == "no_case"
    assert rows["FR-004"]["testcases"] == ["TC-011"]


def test_exec_result_treats_missing_case_as_not_passed():
    """进程中途崩溃导致某条用例没打结果：不能算通过。"""
    rows = rows_of(build(exec_meta=make_exec(missing=("TC-003",))))
    assert rows["FR-001"]["exec_result"] == "partial"
    assert rows["FR-001"]["status"] == trace.ST_EXEC_FAIL


def test_exec_result_ignores_cases_outside_test_impl():
    """exec 里多出来的用例（编号没登记进 test_impl）不参与归属。"""
    rows = rows_of(build(exec_meta=make_exec(extra={"TC-999": "fail"})))
    assert rows["FR-002"]["exec_result"] == "all"


# ---------- 分支覆盖列 ----------

def test_branch_coverage_is_worst_value_over_row_functions():
    """木桶口径：一条需求名下多个函数，取最差的那个，不取平均。"""
    code = {"derived_from": {"comm_pack": ["FR-001"], "comm_ack": ["FR-001"]}}
    ex = make_exec(branch={"comm_pack": 62.5, "comm_ack": 100.0, "comm_crc16": 10.0})
    rows = rows_of(build(code=code, exec_meta=ex))
    assert rows["FR-001"]["branch_coverage"] == 62.5


def test_branch_coverage_rounds_to_two_decimals():
    ex = make_exec(branch={"comm_pack": 66.66666})
    assert rows_of(build(exec_meta=ex))["FR-001"]["branch_coverage"] == 66.67


def test_branch_coverage_skips_functions_without_data():
    """覆盖率值为 None 的函数直接跳过；该行全都没数据时列值为 None。"""
    ex = make_exec(branch={"comm_ack": 88.0, "comm_pack": None})
    rows = rows_of(build(exec_meta=ex))
    assert rows["FR-004"]["branch_coverage"] == 88.0     # comm_ack 归 FR-004
    assert rows["FR-001"]["branch_coverage"] is None     # comm_pack 无数据
    assert rows["FR-002"]["branch_coverage"] is None     # comm_crc16 不在本轮结论里


def test_branch_coverage_falls_back_to_lld_functions():
    """没有代码产物时按详设函数取覆盖率，列不留白。"""
    rows = rows_of(build(code=None, exec_meta=make_exec(branch={"comm_unpack": 71.0})))
    assert rows["FR-003"]["branch_coverage"] == 71.0


# ---------- 状态优先级：执行失败 > 覆盖不足 > 文档四态 ----------

def test_status_ok_when_everything_green():
    rows = rows_of(build())
    assert rows["FR-001"]["status"] == trace.ST_OK
    assert rows["FR-001"]["status_text"] == "贯通"


def test_status_exec_fail_beats_low_coverage():
    """同一行既挂了用例又覆盖不足：只报执行失败（更要命的那个）。"""
    ex = make_exec(fail=("TC-001", "TC-002"), branch={"comm_crc16": 20.0})
    rows = rows_of(build(exec_meta=ex))
    assert rows["FR-002"]["exec_result"] == "none"
    assert rows["FR-002"]["branch_coverage"] == 20.0
    assert rows["FR-002"]["status"] == trace.ST_EXEC_FAIL
    assert rows["FR-002"]["status_text"] == "执行失败：全部失败"


def test_status_exec_fail_beats_document_ok():
    """文档链路全贯通也压不住用例失败：实现与需求对不上是硬结论。"""
    rows = rows_of(build(exec_meta=make_exec(fail=("TC-003",))))
    assert rows["FR-001"]["missing"] == []
    assert rows["FR-001"]["status"] == trace.ST_EXEC_FAIL
    assert rows["FR-001"]["status_text"] == "执行失败：部分失败"


def test_status_low_cov_beats_document_ok():
    ex = make_exec(branch={"comm_crc16": 100.0, "comm_pack": 55.0,
                           "comm_unpack": 100.0})
    rows = rows_of(build(exec_meta=ex))
    assert rows["FR-001"]["status"] == trace.ST_LOW_COV
    assert rows["FR-001"]["status_text"] == "覆盖不足（最低分支覆盖 55.0%，门限 80%）"
    # 达标的行不受牵连
    assert rows["FR-002"]["status"] == trace.ST_OK


def test_status_low_cov_beats_pending_and_gap():
    """验证维度的结论优先于「文档还没写完」，否则缺口会被待生成掩盖。"""
    srs = {"functional_requirements": [
        {"id": "FR-001", "desc": "x", "priority": "P0"},          # 连素材都没标
        {"id": "FR-002", "desc": "y", "priority": "P0", "derived_from": ["OBJ-001"]}]}
    lld = {"derived_from": {"comm_pack": ["FR-001"], "comm_crc16": ["FR-002"]}}
    code = {"derived_from": {"comm_pack": ["FR-001"], "comm_crc16": ["FR-002"]}}
    test_impl = {"cases": [{"id": "TC-003", "fn": "comm_pack", "fr_ids": ["FR-001"]},
                           {"id": "TC-001", "fn": "comm_crc16", "fr_ids": ["FR-002"]}]}
    ex = make_exec(branch={"comm_pack": 40.0, "comm_crc16": 100.0})
    # 只给 SRS + 验证产物：概设/用例都还没产出（文档维度是 pending / gap）
    m = trace.build_matrix(srs, None, lld, None, code_meta=code, static_meta=None,
                           test_impl_meta=test_impl, exec_meta=ex, coverage_min=80.0)
    rows = rows_of(m)
    assert rows["FR-001"]["status"] == trace.ST_LOW_COV
    assert rows["FR-002"]["status"] == trace.ST_PENDING
    assert "概要设计" in rows["FR-002"]["status_text"]


def test_status_counts_include_verification_states():
    ex = make_exec(fail=("TC-001",),
                   branch={"comm_crc16": 100.0, "comm_pack": 50.0,
                           "comm_unpack": 100.0})
    counts = build(exec_meta=ex)["summary"]["status_counts"]
    assert counts.get(trace.ST_EXEC_FAIL) == 1        # FR-002：TC-001 挂、TC-002 过
    assert counts.get(trace.ST_LOW_COV) == 1          # FR-001：comm_pack 50%
    assert counts.get(trace.ST_OK) == 2               # FR-003 达标、FR-004 无关联用例
    assert sum(counts.values()) == len(SRS["functional_requirements"])


def test_coverage_min_gate_is_configurable():
    """门限来自 .env：调高就报覆盖不足，不给门限就不判这一维。"""
    ex = make_exec(branch={"comm_crc16": 100.0, "comm_pack": 85.0,
                           "comm_unpack": 100.0})
    assert rows_of(build(exec_meta=ex, coverage_min=80.0))["FR-001"]["status"] == trace.ST_OK
    assert rows_of(build(exec_meta=ex, coverage_min=90.0))["FR-001"]["status"] == trace.ST_LOW_COV
    # 没配门限（None）：覆盖列照填，但不参与状态判定
    row = rows_of(build(exec_meta=ex, coverage_min=None))["FR-001"]
    assert row["branch_coverage"] == 85.0
    assert row["status"] == trace.ST_OK


def test_no_case_does_not_trigger_exec_fail():
    """「无关联用例」是测试设计缺口，不是执行失败：状态仍走文档维度。

    把 no_case 判成执行失败会把「还没跑」和「跑了挂了」混成一谈，
    归因智能体据此决定回哪个阶段重生，混淆就会改错地方。"""
    rows = rows_of(build())
    assert rows["FR-004"]["exec_result"] == "no_case"
    assert rows["FR-004"]["status"] == trace.ST_OK


def test_not_run_does_not_trigger_exec_fail():
    rows = rows_of(build(exec_meta=None))
    assert rows["FR-001"]["status"] == trace.ST_OK


# ---------- 纯函数级判定 ----------

def test_exec_status_of_returns_none_when_not_applicable():
    s = {"coverage_min": 80.0}
    assert trace.exec_status_of({"exec_result": "all", "branch_coverage": 99.0}, s) is None
    assert trace.exec_status_of({"exec_result": "not_run"}, s) is None
    assert trace.exec_status_of({"exec_result": "no_case"}, s) is None
    assert trace.exec_status_of({}, s) is None
    assert trace.exec_status_of(None, None) is None


def test_exec_status_of_priority():
    s = {"coverage_min": 80.0}
    row = {"exec_result": "partial", "branch_coverage": 10.0}
    assert trace.exec_status_of(row, s) == trace.ST_EXEC_FAIL
    assert trace.exec_status_of({"exec_result": "none"}, s) == trace.ST_EXEC_FAIL
    assert trace.exec_status_of({"branch_coverage": 79.9}, s) == trace.ST_LOW_COV
    assert trace.exec_status_of({"branch_coverage": 80.0}, s) is None   # 门限含等号


def test_row_status_text_carries_verification_detail():
    s = {"coverage_min": 80.0}
    assert trace.row_status_text({"branch_coverage": 10.0}, s) == \
        "覆盖不足（最低分支覆盖 10.0%，门限 80%）"
    assert trace.row_status_text({"exec_result": "partial"}, s) == "执行失败：部分失败"
    assert trace.row_status_text({"exec_result": "none"}, s) == "执行失败：全部失败"


def test_row_status_text_falls_back_for_unknown_exec_value():
    """认不出的执行结果值不构成验证结论：退回文档维度，不硬凑一个「失败」。"""
    text = trace.row_status_text({"exec_result": "weird"}, {})
    assert text == "待补全：素材来源"
    assert trace.exec_status_of({"exec_result": "weird"}, {"coverage_min": 80.0}) is None


def test_row_missing_ignores_verification_columns():
    """文档链路缺口的判定口径不因新增四列而改变。"""
    rows_a = rows_of(trace.build_matrix(SRS, HLD, LLD, TC))
    rows_b = rows_of(build())
    assert {k: v["missing"] for k, v in rows_a.items()} == \
           {k: v["missing"] for k, v in rows_b.items()}


# ---------- 概览标志位 ----------

def test_summary_flags_reflect_artifact_presence():
    s = build()["summary"]
    assert (s["has_code"], s["has_static"], s["has_test_impl"], s["has_exec"]) == \
        (True, True, True, True)
    assert s["coverage_min"] == 80.0

    s2 = trace.build_matrix(SRS, HLD, LLD, TC)["summary"]
    assert (s2["has_code"], s2["has_static"], s2["has_test_impl"], s2["has_exec"]) == \
        (False, False, False, False)
    assert s2["coverage_min"] is None

    s3 = build(code=None, static=None, test_impl=None, exec_meta=None)["summary"]
    assert not any(s3[k] for k in ("has_code", "has_static", "has_test_impl", "has_exec"))
    assert s3["has_tc"] is True


def test_exec_text_covers_every_exec_result_value():
    """EXEC_TEXT 必须覆盖全部取值，否则前端会露出英文原文。"""
    for value in ("all", "partial", "none", "not_run", "no_case"):
        assert value in trace.EXEC_TEXT
    assert trace.EXEC_TEXT["no_case"] == "无关联用例"


# ---------- 与真实样例夹具对齐 ----------

def _sample_metas():
    """按 tests/fixtures/comm_sample 拼一整套产物元数据（19 条用例 / 6 个函数）。"""
    frs = sorted({fr for _cid, _fn, refs in SAMPLE.CASE_FR for fr in refs})
    srs = {"functional_requirements": [
        {"id": f, "desc": "需求 " + f, "priority": "P0", "derived_from": ["OBJ-001"]}
        for f in frs]}
    by_fn = {}
    for _cid, fn, refs in SAMPLE.CASE_FR:
        bucket = by_fn.setdefault(fn, [])
        for r in refs:
            if r not in bucket:
                bucket.append(r)
    derived = {f["name"]: list(by_fn.get(f["name"], []))
               for f in SAMPLE.LLD_FUNCTIONS}
    lld = {"functions": [{"name": f["name"], "sig": f["sig"]}
                         for f in SAMPLE.LLD_FUNCTIONS],
           "derived_from": {k: list(v) for k, v in derived.items()}}
    tc = {"testcases": [{"id": cid, "fr_ids": list(refs)}
                        for cid, _fn, refs in SAMPLE.CASE_FR]}
    code = {"files": sorted(SAMPLE.CODE_FILES),
            "derived_from": {k: list(v) for k, v in derived.items()}}
    static = {"ok": True, "required": 0, "advisory": 0,
              "functions": [{"name": f["name"]} for f in SAMPLE.LLD_FUNCTIONS],
              "violations": []}
    test_impl = {"files": sorted(SAMPLE.TEST_FILES),
                 "cases": [{"id": cid, "fn": fn, "fr_ids": list(refs)}
                           for cid, fn, refs in SAMPLE.CASE_FR]}
    hld = {"modules": ["comm"], "derived_from": {"comm": list(frs)}}
    return srs, hld, lld, tc, code, static, test_impl


def sample_exec(fail=(), branch=None):
    """样例模块的 exec 结论：默认 19 条全过、6 个函数分支覆盖 100%。"""
    cov = {f["name"]: 100.0 for f in SAMPLE.LLD_FUNCTIONS}
    cov.update(branch or {})
    return {"tests": {"by_id": {cid: {"id": cid,
                                      "status": "fail" if cid in fail else "pass"}
                                for cid in SAMPLE.CASE_IDS}},
            "coverage": {"function_map": {n: {"name": n, "file": "src/comm.c",
                                              "in_scope": True,
                                              "branch_effective": p}
                                          for n, p in cov.items()}}}


def test_sample_module_full_chain_is_green():
    srs, hld, lld, tc, code, static, test_impl = _sample_metas()
    m = trace.build_matrix(srs, hld, lld, tc, code_meta=code, static_meta=static,
                           test_impl_meta=test_impl, exec_meta=sample_exec(),
                           coverage_min=80.0)
    assert len(m["rows"]) == 6
    for row in m["rows"]:
        assert row["code_units"], row["id"]
        assert row["static_violations"] == 0
        assert row["exec_result"] == "all"
        assert row["branch_coverage"] == 100.0
        assert row["status"] == trace.ST_OK, (row["id"], row["status_text"])
    assert m["summary"]["status_counts"] == {trace.ST_OK: 6}


def test_sample_module_crc_defect_shows_up_on_its_requirement():
    """注入 CRC 初值缺陷（TC-001/TC-002 挂）：只有 FR-002 那一行报执行失败。

    端到端验收要求「失败能被归到具体需求上」，这一步就是它的离线等价判定。"""
    srs, hld, lld, tc, code, static, test_impl = _sample_metas()
    ex = sample_exec(fail=("TC-001", "TC-002"), branch={"comm_crc16": 62.5})
    m = trace.build_matrix(srs, hld, lld, tc, code_meta=code, static_meta=static,
                           test_impl_meta=test_impl, exec_meta=ex, coverage_min=80.0)
    rows = rows_of(m)
    assert rows["FR-002"]["status"] == trace.ST_EXEC_FAIL
    assert rows["FR-002"]["exec_result"] == "none"
    assert rows["FR-002"]["branch_coverage"] == 62.5
    # 其余需求不受牵连：CRC 缺陷只影响校验相关的那条
    for fid in ("FR-001", "FR-003", "FR-004", "FR-005", "FR-006"):
        assert rows[fid]["status"] == trace.ST_OK, (fid, rows[fid]["status_text"])


def test_sample_module_static_violation_lands_on_its_requirement():
    """静态违规按函数归到需求：comm_pack 的违规只影响 FR-001 / FR-004 / FR-005。"""
    srs, hld, lld, tc, code, static, test_impl = _sample_metas()
    static = dict(static, ok=False, required=1,
                  violations=[{"rule_id": "WB-C-005", "func": "comm_pack",
                               "severity": "required"}])
    m = trace.build_matrix(srs, hld, lld, tc, code_meta=code, static_meta=static,
                           test_impl_meta=test_impl, exec_meta=sample_exec(),
                           coverage_min=80.0)
    rows = rows_of(m)
    assert rows["FR-001"]["static_violations"] == 1
    assert rows["FR-002"]["static_violations"] == 0
    assert m["summary"]["static_unattributed"] == 0
    # 静态违规不改变行状态（它走评审门与偏差单，不在矩阵状态里判定）
    assert rows["FR-001"]["status"] == trace.ST_OK


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
