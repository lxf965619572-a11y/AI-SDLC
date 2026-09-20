"""离线 C 产物（core.mock_c）的测试：合格样本 / 往返抽取 / 元数据 / 归因结论。

第一性原理：mock_c 是「离线演示跑的代码」与「单测里的基准代码」的同一份来源，
它必须真的满足系统对被测代码的全部硬性要求，否则离线绿灯就是假绿。这里钉四条：
  1. 合格样本：CODE_FILES 配 ORDER_FUNCTIONS 设计基线过 c_static 零违规——
     这是 mock 敢声称「受限子集合格样本」的唯一凭据（测试文件的 main 圈复杂度
     故意很高，但测试不进静态门，流水线只对 code 产物跑 c_static）；
  2. 往返一致：code_markdown / test_impl_markdown 经 c_files.extract_files 能
     原样还原源文件，末尾 ```json 元数据可解析且字段齐全；
  3. 追溯分配单一事实：derived_from 的函数→需求分配与 spread 分桶一致，
     code_markdown 沿用详细设计给定的分配、缺键才回退，防止矩阵两列各说一套；
  4. 用例登记不造假：test_impl_markdown 只登记「既在设计用例表、又真的出现在
     测试源码里」的编号，mock 不会凭空造出没有实现的用例。

零第三方依赖、可离线运行：
    .venv\\Scripts\\python.exe tests\\test_mock_c.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import c_files, c_static, mock_c

_JSON_RE = re.compile(r"```json\n(.*?)\n```", re.S)


def _meta(markdown):
    """取生成物末尾 ```json 元数据块并解析。"""
    m = _JSON_RE.search(markdown)
    assert m, "生成物缺少 ```json 元数据块"
    return json.loads(m.group(1))


FRS6 = ["FR-001", "FR-002", "FR-003", "FR-004", "FR-005", "FR-006"]


# ==================== 合格样本：静态零违规 ====================
def test_code_files_pass_static_with_zero_violations():
    """mock 自称「受限子集合格样本」，就必须真的零违规（含设计覆盖）。"""
    rep = c_static.check_files(mock_c.CODE_FILES, {"complexity_max": 10},
                               mock_c.ORDER_FUNCTIONS)
    assert rep["ok"] is True
    assert rep["total"] == 0 and rep["required"] == 0 and rep["advisory"] == 0, \
        [(v["rule_id"], v["file"], v["line"], v["message"]) for v in rep["violations"]]


def test_code_files_implement_every_designed_function():
    """设计基线里的 3 个对外函数都在代码中实现，签名与详细设计一致。"""
    rep = c_static.check_files(mock_c.CODE_FILES, {}, mock_c.ORDER_FUNCTIONS)
    by_name = {f["name"]: f for f in rep["functions"]}
    for spec in mock_c.ORDER_FUNCTIONS:
        assert spec["name"] in by_name, spec["name"]
    # 无 WB-D-001/002/003：设计↔实现对照干净
    assert not [v for v in rep["violations"] if v["rule_id"].startswith("WB-D")]


def test_code_and_test_files_use_only_convention_paths():
    """所有路径都落在约定目录，illegal_paths 为空——否则同步了也不会被编译。"""
    assert c_files.illegal_paths(mock_c.CODE_FILES) == []
    assert c_files.illegal_paths(mock_c.TEST_FILES) == []
    assert c_files.file_list(mock_c.CODE_FILES) == ["include/order.h", "src/order.c"]
    assert c_files.file_list(mock_c.TEST_FILES) == ["tests/test_order.c"]


def test_test_source_covers_all_eight_cases_and_uses_harness():
    present = sorted(set(re.findall(r'"(TC-\d+)"', mock_c.TEST_ORDER_C)))
    assert present == ["TC-00%d" % i for i in range(1, 9)]
    assert sorted(mock_c.CASE_FN) == present
    # 测试只依赖头文件与测试桩（黑盒），不读实现
    assert '#include "order.h"' in mock_c.TEST_ORDER_C
    assert '#include "wb_harness.h"' in mock_c.TEST_ORDER_C
    assert "wb_begin" in mock_c.TEST_ORDER_C and "wb_summary" in mock_c.TEST_ORDER_C


# ==================== code_markdown 往返 ====================
def test_code_markdown_roundtrips_source_files():
    md = mock_c.code_markdown(FRS6)
    got = c_files.extract_files(md)
    assert set(got) == set(mock_c.CODE_FILES)
    for k in mock_c.CODE_FILES:
        assert got[k] == mock_c.CODE_FILES[k].strip("\n"), k


def test_code_markdown_meta_has_module_files_derived():
    meta = _meta(mock_c.code_markdown(FRS6))
    assert set(meta) == {"module", "files", "derived_from"}
    assert meta["module"] == "order"
    assert meta["files"] == c_files.file_list(mock_c.CODE_FILES)
    assert set(meta["derived_from"]) == {f["name"] for f in mock_c.ORDER_FUNCTIONS}


def test_code_markdown_default_derived_matches_derived_from():
    meta = _meta(mock_c.code_markdown(FRS6))
    assert meta["derived_from"] == mock_c.derived_from(FRS6)


def test_code_markdown_reuses_given_derived_when_keys_complete():
    """详细设计给定的实现对照表原样沿用：代码是设计的落地，不该重新洗牌。"""
    names = {f["name"] for f in mock_c.ORDER_FUNCTIONS}
    given = {n: ["FR-900"] for n in names}
    meta = _meta(mock_c.code_markdown(FRS6, derived=given))
    assert meta["derived_from"] == given


def test_code_markdown_falls_back_when_derived_keys_incomplete():
    """缺键或键名不符 → 退回按 frs 重新分配，避免矩阵出现无主函数。"""
    partial = {"order_init": ["FR-X"]}          # 缺 order_create / order_pay
    meta = _meta(mock_c.code_markdown(FRS6, derived=partial))
    assert meta["derived_from"] == mock_c.derived_from(FRS6)
    assert set(meta["derived_from"]) == {f["name"] for f in mock_c.ORDER_FUNCTIONS}


# ==================== test_impl_markdown 往返 ====================
def test_test_impl_markdown_roundtrips_source_files():
    md = mock_c.test_impl_markdown({"TC-001": ["FR-001"]})
    got = c_files.extract_files(md)
    assert set(got) == set(mock_c.TEST_FILES)
    for k in mock_c.TEST_FILES:
        assert got[k] == mock_c.TEST_FILES[k].strip("\n"), k


def test_test_impl_meta_registers_only_real_and_designed_cases():
    """只登记「既在设计用例表、又真的出现在测试源码里」的编号。"""
    case_frs = {"TC-001": ["FR-1"], "TC-008": ["FR-8"],
                "TC-099": ["FR-9"],              # 设计里有，源码没有 → 丢弃
                "TC-002": []}                     # 空 fr_ids 也登记
    meta = _meta(mock_c.test_impl_markdown(case_frs))
    ids = [c["id"] for c in meta["cases"]]
    assert "TC-099" not in ids, "mock 不得凭空造出源码里没有的用例"
    assert set(ids) == {"TC-001", "TC-002", "TC-008"}
    assert ids == sorted(ids)


def test_test_impl_meta_case_shape_and_fn_mapping():
    case_frs = {"TC-001": ["FR-1", "FR-2"], "TC-003": ["FR-3"]}
    meta = _meta(mock_c.test_impl_markdown(case_frs))
    by_id = {c["id"]: c for c in meta["cases"]}
    assert by_id["TC-001"] == {"id": "TC-001", "fn": mock_c.CASE_FN["TC-001"],
                               "fr_ids": ["FR-1", "FR-2"]}
    assert by_id["TC-003"]["fn"] == mock_c.CASE_FN["TC-003"]
    assert meta["files"] == c_files.file_list(mock_c.TEST_FILES)


def test_every_source_case_has_case_fn_entry_so_default_is_pure_defense():
    """源码里出现的每个编号都在 CASE_FN 中有映射，DEFAULT_FN 只是兜底常量。

    换言之 test_impl_markdown 永远不会真的落到 DEFAULT_FN——这条把「兜底不可达」
    固化下来：将来若新增用例却忘了登记 CASE_FN，fn 会静默退化成 DEFAULT_FN，
    追溯矩阵的函数列就会指错，这里先断言映射表覆盖完整。"""
    present = set(re.findall(r'"(TC-\d+)"', mock_c.TEST_ORDER_C))
    assert present <= set(mock_c.CASE_FN), present - set(mock_c.CASE_FN)
    assert mock_c.DEFAULT_FN == "order_create"
    # 兜底常量本身必须是被测函数之一，否则退化时会指向不存在的函数
    assert mock_c.DEFAULT_FN in {f["name"] for f in mock_c.ORDER_FUNCTIONS}


# ==================== derived_from / spread 分桶 ====================
def test_spread_round_robins_items_into_buckets():
    assert mock_c.spread(["a", "b", "c"], 3) == [["a"], ["b"], ["c"]]
    assert mock_c.spread(["a", "b", "c", "d"], 3) == [["a", "d"], ["b"], ["c"]]


def test_spread_dedups_within_bucket_and_handles_edge_n():
    assert mock_c.spread(["a", "b", "a"], 1) == [["a", "b"]]   # 同桶去重
    assert mock_c.spread([], 3) == [[], [], []]
    assert mock_c.spread(["a"], 0) == []                        # n<=0 → 空


def test_derived_from_covers_every_function_name():
    d = mock_c.derived_from(FRS6)
    assert set(d) == {f["name"] for f in mock_c.ORDER_FUNCTIONS}
    # 6 条需求轮流分到 3 个函数：每个函数 2 条
    assert d == {"order_init": ["FR-001", "FR-004"],
                 "order_create": ["FR-002", "FR-005"],
                 "order_pay": ["FR-003", "FR-006"]}


def test_derived_from_empty_frs_yields_empty_lists():
    d = mock_c.derived_from([])
    assert set(d) == {f["name"] for f in mock_c.ORDER_FUNCTIONS}
    assert all(v == [] for v in d.values())


# ==================== struct_block ====================
def test_struct_block_extracts_typedef_region():
    sb = mock_c.struct_block()
    lines = sb.splitlines()
    assert lines[0].startswith("typedef struct {")
    assert lines[-1].startswith("} OrderCtx;")
    for token in ("OrderItem", "} Order;", "PayResult", "InventorySlot", "OrderCtx"):
        assert token in sb, token


def test_struct_block_is_a_substring_of_the_header():
    """文档直接引用头文件里的 struct，而不是另抄一份，杜绝字段漂移。"""
    assert mock_c.struct_block() in mock_c.ORDER_H


# ==================== attribution_markdown ====================
def test_attribution_markdown_is_parseable_with_decision():
    for decision in ("fix_code", "fix_test"):
        meta = _meta(mock_c.attribution_markdown(decision))
        assert meta["decision"] == decision
        assert set(meta) == {"decision", "reason", "evidence", "hint"}
        assert isinstance(meta["evidence"], list) and isinstance(meta["hint"], list)


def test_attribution_markdown_defaults_to_fix_code():
    meta = _meta(mock_c.attribution_markdown())
    assert meta["decision"] == "fix_code"


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
