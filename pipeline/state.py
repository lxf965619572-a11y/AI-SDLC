"""LangGraph 流水线状态定义。"""
from typing import Annotated, Any, TypedDict


def _merge_dict(left: dict, right: dict) -> dict:
    out = dict(left or {})
    out.update(right or {})
    return out


class PipelineState(TypedDict, total=False):
    """跨节点流转的状态。artifacts 按阶段累积。"""
    project_id: int
    # 各阶段产物：{stage: {"id": artifact_id, "version": n}}
    artifacts: Annotated[dict, _merge_dict]
    # 结构化原始数据（阶段1结果，供下游引用）
    structured: dict
    srs_meta: dict
    hld_meta: dict
    # 驳回时的评审意见（gate 写入，agent 节点读取后清空）
    retry_comments: str | None
    current_stage: str
    error: str | None
    # ---- 代码验证闭环 ----
    # 自动修复轮数：{"static": n, "exec": n}。超过 config.MAX_FIX_ROUNDS 即转人工，
    # 计数放在状态里而不是数据库，是为了让「这一版流水线跑了几轮」随检查点一起恢复。
    fix_rounds: Annotated[dict, _merge_dict]
    # 最近一次失败归因结论：{"decision", "reason", "source"}，人工裁决时要看
    attribution: dict
    # 只改代码不改测试时，静态检查通过后直接回 exec，跳过 test_impl 重生与其评审门。
    # 取值 "exec" 表示跳过，None 表示正常走 test_impl。
    resume_after_code: str | None
    # 工具节点（static/exec）的路由决策。路由函数在 LangGraph 里只能读状态，
    # 判据必须在节点里算完写进来，否则检查点重放时同一条结论可能走到不同分支。
    tool_route: str | None
