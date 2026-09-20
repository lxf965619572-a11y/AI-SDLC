"""LangGraph 图构建。

形状对应 V 模型的左右两臂加一条验证回路：
  parse → [agent → gate] × 4（需求/概要/详细/测试用例设计）
        → agent_code → gate_code → static_node ─┐
        → agent_test_impl → gate_test_impl → exec_node → report_node → END
静态检查与执行验证是工具节点，判据即结论，不设文风评审门；但结论不通过时
一定要落到 gate_static / gate_exec 由人裁决，不允许静默放行。
门控用 interrupt()，检查点落 SQLite（跨进程重启可恢复）。"""
from langgraph.graph import END, START, StateGraph

from pipeline import nodes
from pipeline.state import PipelineState


def build_graph(checkpointer):
    g = StateGraph(PipelineState)
    g.add_node("parse_input", nodes.parse_input)
    g.add_edge(START, "parse_input")
    g.add_edge("parse_input", "agent_requirement")

    for stage in nodes.STAGES:
        g.add_node(f"agent_{stage}", nodes.make_agent(stage))
        g.add_node(f"gate_{stage}", nodes.make_gate(stage))
        g.add_edge(f"agent_{stage}", f"gate_{stage}")
        g.add_conditional_edges(
            f"gate_{stage}", nodes.make_route(stage),
            {"next": nodes.next_stage_or_end(stage), "redo": f"agent_{stage}"},
        )

    # ---- 静态检查回路：code 评审通过 → static →（整改/偏差/继续）----
    g.add_node("static_node", nodes.static_node)
    g.add_conditional_edges("static_node", nodes.route_static, nodes.STATIC_ROUTE_MAP)
    g.add_node("gate_static", nodes.gate_static)
    g.add_conditional_edges("gate_static", nodes.route_static_gate,
                            nodes.STATIC_GATE_ROUTE_MAP)

    # ---- 执行验证回路：test_impl 评审通过 → exec →（改代码/改测试/转人工/出报告）----
    g.add_node("exec_node", nodes.exec_node)
    g.add_conditional_edges("exec_node", nodes.route_exec, nodes.EXEC_ROUTE_MAP)
    g.add_node("gate_exec", nodes.gate_exec)
    g.add_conditional_edges("gate_exec", nodes.route_exec_gate,
                            nodes.EXEC_GATE_ROUTE_MAP)

    g.add_node("report_node", nodes.report_node)
    g.add_edge("report_node", END)
    return g.compile(checkpointer=checkpointer)
