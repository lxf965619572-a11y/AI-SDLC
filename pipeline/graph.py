"""LangGraph 图构建：parse → [agent → gate] × 4 → END。
门控用 interrupt()，检查点落 SQLite（跨进程重启可恢复）。"""
from langgraph.graph import END, START, StateGraph

from pipeline import nodes_v2 as nodes
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

    # 最后一个 gate 的 next 分支由 next_stage_or_end 返回 "__end__"
    return g.compile(checkpointer=checkpointer)
