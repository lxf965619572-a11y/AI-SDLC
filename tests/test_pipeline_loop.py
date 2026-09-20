"""失败闭环的图级路由测试：不连验证机、不调模型，只验回路的形状与轮数上限。

为什么这一层要单测：闭环是 exec 失败 → 回代码/测试阶段重生 → 过评审门 →
（改代码时）静态检查 → 再回 exec。回路上夹着人工评审门，而人工裁决会把自动修复
额度清零；一旦「通过也清零」，MAX_FIX_ROUNDS 就永远数不满，超限的问题报告单与
人工裁决入口形同虚设，失败可以靠「重生→过门→再失败」无限循环下去。

真实验证机跑一轮要几十秒且依赖网络，而这条回路的形状完全由路由函数与路由表决定，
所以这里用真实路由（nodes.route_*、*_ROUTE_MAP、gate_fix_rounds）走状态机，
只把工具判据换成替身。判据本身的对错由 tests/test_executor.py 与
scripts/e2e_pipeline_mock.py 负责。

用法：.venv\\Scripts\\python.exe tests\\test_pipeline_loop.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                     # noqa: E402
from pipeline import nodes                                         # noqa: E402
from verification import executor                                  # noqa: E402

# 工具节点与工具裁决门的路由：函数 + 路由表，全部取自 nodes，不在测试里另写一套
_TOOL_ROUTES = {
    "static_node": (nodes.route_static, nodes.STATIC_ROUTE_MAP),
    "exec_node": (nodes.route_exec, nodes.EXEC_ROUTE_MAP),
    "gate_static": (nodes.route_static_gate, nodes.STATIC_GATE_ROUTE_MAP),
    "gate_exec": (nodes.route_exec_gate, nodes.EXEC_GATE_ROUTE_MAP),
}

# exec 每一轮的结论替身：全绿 / 判为代码缺陷 / 判为测试缺陷 / 环境不可用
OK, FAIL_CODE, FAIL_TEST, BLOCKED = "ok", "fail_code", "fail_test", "blocked"
_DECISION = {FAIL_CODE: executor.DECISION_FIX_CODE, FAIL_TEST: executor.DECISION_FIX_TEST}


def _human_decision(human, stage: str, visit: int) -> str:
    """人工裁决替身：字符串 = 全程一个态度；字典 = 按阶段指定；
    可调用 = 按 (阶段, 第几次到这个门) 决定，用来模拟「先驳回一次再受理」。"""
    if callable(human):
        return human(stage, visit)
    if isinstance(human, dict):
        return human.get(stage, "approve")
    return human


def walk(exec_kinds=(OK,), static_kind=OK, human="approve", start="exec_node",
         max_steps=200):
    """按真实路由走图，返回 (走过的节点序列, 终态)。

    节点副作用只保留与路由有关的状态字段（fix_rounds / tool_route /
    retry_comments / resume_after_code / attribution），判据结论用入参替身。
    exec_kinds 用尽后重复最后一项，正好模拟「一直修不好」。"""
    state = {"project_id": 1, "artifacts": {}, "fix_rounds": {}, "tool_route": None,
             "retry_comments": None, "resume_after_code": None, "attribution": None,
             "current_stage": None}
    runs = {"exec": 0, "static": 0}
    visits = {}
    path = []
    current = start

    def _bump(key, delta=1):
        fr = dict(state.get("fix_rounds") or {})
        fr[key] = int(fr.get(key) or 0) + delta
        state["fix_rounds"] = fr          # 状态里的 fix_rounds 是合并语义

    for _ in range(max_steps):
        path.append(current)
        if current in ("report_node", "__end__"):
            return path, state

        if current == "exec_node":
            kind = exec_kinds[min(runs["exec"], len(exec_kinds) - 1)]
            runs["exec"] += 1
            rounds = int((state.get("fix_rounds") or {}).get("exec") or 0)
            ok = kind == OK
            over_limit = (not ok) and kind != BLOCKED and rounds >= config.MAX_FIX_ROUNDS
            decision = _DECISION.get(kind)
            state.update({"current_stage": "exec", "resume_after_code": None,
                          "retry_comments": None})
            if decision:
                state["attribution"] = {"decision": decision, "source": "agent"}
            if ok:
                state["tool_route"] = "next"
            elif kind == BLOCKED or over_limit:
                state["tool_route"] = "gate"
            else:
                state["tool_route"] = ("fix_test" if decision == executor.DECISION_FIX_TEST
                                       else "fix_code")
                state["retry_comments"] = "整改要求（含工具链事实）"
                _bump("exec")
                if state["tool_route"] == "fix_code":
                    # 只改代码：静态过了直接回执行验证，不重生测试、不过测试评审门
                    state["resume_after_code"] = "exec"
        elif current == "static_node":
            runs["static"] += 1
            rounds = int((state.get("fix_rounds") or {}).get("static") or 0)
            ok = static_kind == OK
            over_limit = (not ok) and rounds >= config.MAX_FIX_ROUNDS
            state["current_stage"] = "static"
            if ok:
                state["tool_route"] = ("skip" if state.get("resume_after_code") == "exec"
                                       else "next")
                state["retry_comments"] = None
            elif over_limit:
                state["tool_route"] = "gate"
                state["retry_comments"] = None
            else:
                state["tool_route"] = "regen"
                state["retry_comments"] = "静态整改要求"
                _bump("static")
        elif current.startswith("gate_"):
            stage = current[len("gate_"):]
            visits[stage] = visits.get(stage, 0) + 1
            approved = _human_decision(human, stage, visits[stage]) == "approve"
            state["retry_comments"] = None if approved else "驳回意见"
            reset = nodes.gate_fix_rounds(stage, approved)
            if reset is not None:
                state["fix_rounds"] = {**(state.get("fix_rounds") or {}), **reset}
        elif current.startswith("agent_"):
            state["retry_comments"] = None       # 重生节点消费掉整改要求
        else:
            raise AssertionError("未预期的节点：" + current)

        if current in _TOOL_ROUTES:
            fn, table = _TOOL_ROUTES[current]
            current = table[fn(state)]
        elif current.startswith("gate_"):
            stage = current[len("gate_"):]
            current = (f"agent_{stage}" if state.get("retry_comments")
                       else nodes.next_stage_or_end(stage))
        elif current.startswith("agent_"):
            current = "gate_" + current[len("agent_"):]
        else:
            raise AssertionError("没有路由的节点：" + current)
    raise AssertionError("走了 %d 步仍未收敛，失败闭环失控：%s" % (max_steps, path))


# ================= 评审门与自动修复额度 =================
def test_document_gate_keeps_budget_when_approved():
    """代码/测试实现评审门通过 = 放行自动闭环的下一轮，额度必须接着算。"""
    assert nodes.gate_fix_rounds("code", True) is None
    assert nodes.gate_fix_rounds("test_impl", True) is None


def test_document_gate_restores_budget_when_rejected():
    """人工驳回是重新给基线：一次驳回不该吃掉整轮自动整改额度。"""
    for stage in ("code", "test_impl"):
        assert nodes.gate_fix_rounds(stage, False) == {"static": 0, "exec": 0}, stage


def test_tool_gate_restores_budget_on_both_decisions():
    """偏差单放行与问题报告单受理都是人工对工具结论表态，两侧都重新给额度。"""
    for stage in nodes.TOOL_GATE_STAGES:
        for approved in (True, False):
            assert nodes.gate_fix_rounds(stage, approved) == {"static": 0, "exec": 0}


def test_stages_without_auto_fix_are_untouched():
    for stage in ("parse", "requirement", "hld", "lld", "testcase", "report"):
        assert nodes.gate_fix_rounds(stage, True) is None, stage
        assert nodes.gate_fix_rounds(stage, False) is None, stage


# ================= 回环形状 =================
def test_green_run_goes_straight_to_report():
    path, state = walk([OK])
    assert path == ["exec_node", "report_node"]
    assert state["tool_route"] == "next"


def test_repeated_failure_hits_round_limit_and_lands_on_human_gate():
    """一直修不好：数满 MAX_FIX_ROUNDS 轮自动整改后必须停在人工门，不许无限循环。

    这条是 gate_fix_rounds 语义的兜底——评审门通过若把计数清零，exec 轮数就永远
    数不满，这里会直接抛「回环失控」。"""
    path, state = walk([FAIL_CODE])
    limit = config.MAX_FIX_ROUNDS
    assert path.count("exec_node") == limit + 1, path
    assert path.count("agent_code") == limit, path        # 只有前 limit 轮去重生
    assert path.count("gate_code") == limit, path         # 每次改代码都再过一次评审门
    assert path[-2:] == ["gate_exec", "report_node"], path
    assert state["retry_comments"] is None                # 人工受理，不再回炉
    assert state["fix_rounds"].get("exec") == 0           # 裁决后额度重新给满


def test_over_limit_run_stops_at_gate_when_human_rejects():
    """人工在超限门上驳回一次：按归因责任方回代码阶段，并重新给满一轮自动整改额度。

    驳回不是终点。人工裁决完，自动闭环要能重新数满 MAX_FIX_ROUNDS 轮，
    否则超限之后每次重生都会立刻再判超限，人工再也拿不到一次完整的自动整改。"""
    limit = config.MAX_FIX_ROUNDS
    path, state = walk([FAIL_CODE],
                       human=lambda stage, n: "reject" if (stage == "exec" and n == 1)
                       else "approve")
    assert path.count("gate_exec") == 2, path             # 驳回一次、受理一次
    first = path.index("gate_exec")
    assert path[first + 1] == "agent_code", path          # 判为代码缺陷 → 回代码阶段
    assert path.count("exec_node") == 2 * (limit + 1), path
    assert path[-2:] == ["gate_exec", "report_node"], path
    assert state["fix_rounds"].get("exec") == 0


def test_test_side_failure_regenerates_tests_not_code():
    path, _ = walk([FAIL_TEST])
    assert "agent_test_impl" in path and "agent_code" not in path, path
    assert "gate_test_impl" in path                       # 测试实现也有评审门
    assert path.count("exec_node") == config.MAX_FIX_ROUNDS + 1
    assert path[-2:] == ["gate_exec", "report_node"]


def test_code_side_fix_skips_test_stage_and_its_gate():
    """改代码不改测试：静态检查通过后直接回执行验证，不惊动测试实现评审门。"""
    path, _ = walk([FAIL_CODE, OK])
    assert path == ["exec_node", "agent_code", "gate_code", "static_node",
                    "exec_node", "report_node"], path
    assert "agent_test_impl" not in path and "gate_test_impl" not in path


def test_second_round_green_never_reaches_the_limit_gate():
    path, state = walk([FAIL_CODE, OK])
    assert "gate_exec" not in path, path                  # 修好了就不该出人工裁决门
    assert state["fix_rounds"].get("exec") == 1           # 额度消耗留痕，供报告引用


def test_blocked_environment_goes_to_human_gate_at_once():
    """验证机不可用不算「修一轮再试」：直接转人工，不消耗自动整改额度。"""
    path, state = walk([BLOCKED])
    assert path == ["exec_node", "gate_exec", "report_node"], path
    assert not (state.get("fix_rounds") or {}).get("exec")


def test_static_over_limit_produces_deviation_gate():
    """必查项违规同样先自动整改 MAX_FIX_ROUNDS 轮，超限走偏差单人工裁决。"""
    path, _ = walk([OK], static_kind="fail", start="static_node")
    limit = config.MAX_FIX_ROUNDS
    assert path.count("static_node") == limit + 1, path
    assert path.count("agent_code") == limit, path
    gate = path.index("gate_static")
    # limit 轮「整改→过门」之后，再判一次仍然违规，这次不再重生，直接出偏差单
    assert path[:gate] == (["static_node", "agent_code", "gate_code"] * limit
                           + ["static_node"]), path[:gate]
    assert path[gate + 1] == "agent_test_impl", path      # 偏差单受理后继续往下走


def test_route_maps_point_at_real_graph_nodes():
    """路由表里写错一个节点名，图要到运行时才炸；这里对着编译好的图核一遍。"""
    from pipeline.graph import build_graph

    known = set(build_graph(None).nodes)
    tables = dict(nodes.STATIC_ROUTE_MAP), dict(nodes.EXEC_ROUTE_MAP)
    for extra in (nodes.STATIC_GATE_ROUTE_MAP, nodes.EXEC_GATE_ROUTE_MAP,
                  dict(nodes.GATE_NEXT)):
        tables += (extra,)
    for table in tables:
        for key, target in table.items():
            assert target in known, f"{key} → {target} 不是图里的节点"


# ================= 回灌文本与问题报告单 =================
def _failed_outcome():
    return {
        "ok": False, "reason": "2 条用例未通过",
        "verdict": {"build": "ok", "tests": "fail", "coverage": "ok"},
        "build": {"ok": True, "diagnostics": []},
        "tests": {"total": 8, "passed": 6, "failed": 2, "crashed": False,
                  "results": [{"id": "TC-006", "status": "fail",
                               "detail": "折扣计算错误 金卡=9000 期望 9500"}]},
        "coverage": {"totals": {"line_pct": 100.0, "lines_hit": 104, "lines_total": 104,
                                "branch_pct": 98.78, "branch_taken": 70,
                                "branch_total": 70}},
    }


def test_exec_feedback_pins_the_responsibility_side():
    """回灌不能是盲改：责任方、工具链事实、以及「不许改用例迁就实现」都要写死。"""
    out = _failed_outcome()
    code_fb = nodes._exec_feedback(out, executor.DECISION_FIX_CODE, "折扣算错",
                                   {"hint": ["核对 discount_cent"]})
    assert "被测代码" in code_fb and executor.DECISION_FIX_CODE in code_fb
    assert "核对 discount_cent" in code_fb
    assert "不得修改任何测试用例" in code_fb
    # 工具链原始判据必须原样带上，模型不能与事实矛盾
    assert "TC-006" in code_fb and "折扣计算错误" in code_fb
    test_fb = nodes._exec_feedback(out, executor.DECISION_FIX_TEST, "用例判据写错", None)
    assert "测试代码" in test_fb and "只修改测试代码" in test_fb
    assert "不得修改任何测试用例" not in test_fb


def test_problem_block_records_rounds_and_escalates():
    md = nodes._problem_block("验证执行未通过", "2 条用例未通过", config.MAX_FIX_ROUNDS,
                              "TC-006 FAIL")
    assert "问题报告单" in md and "TC-006 FAIL" in md
    assert str(config.MAX_FIX_ROUNDS) in md
    assert "转人工评审门裁决" in md            # 超限不许静默放行


def test_deviation_block_separates_waivable_from_bottom_line():
    report = {"violations": [
        {"rule_id": "WB-C-001", "title": "禁止动态内存", "severity": "required",
         "waivable": False, "file": "src/order.c", "line": 12, "message": "malloc"},
        {"rule_id": "WB-C-006", "title": "圈复杂度上限", "severity": "required",
         "waivable": True, "file": "src/order.c", "line": 40, "message": "复杂度 12"},
    ]}
    md = nodes._deviation_block(report, config.MAX_FIX_ROUNDS)
    assert "偏差单" in md and "WB-C-001" in md and "WB-C-006" in md
    assert "不可（安全性底线）" in md          # 不可偏差项：人工也无权放行
    assert "人工批准后方可放行" in md


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
