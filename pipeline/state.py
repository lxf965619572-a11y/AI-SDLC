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
