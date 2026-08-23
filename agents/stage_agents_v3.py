"""简化后的 stage_agents：基于配置驱动，消除重复代码。

对比：
- 旧版本：230 行，4 个几乎相同的函数
- 新版本：50 行，统一调用 execute_stage()
"""
import json
from typing import Callable, Optional

from agents.stage_config import execute_stage, get_stage_config


def analyze_requirements(
    structured_json: str,
    retry_comments: Optional[str] = None,
    previous_markdown: Optional[str] = None,
    progress_cb: Optional[Callable] = None
) -> tuple[str, dict]:
    """需求分析阶段"""
    config = get_stage_config("requirement")
    return execute_stage(
        config=config,
        input_data={"structured_json": structured_json},
        retry_comments=retry_comments,
        previous_markdown=previous_markdown,
        progress_cb=progress_cb
    )


def design_hld(
    srs_markdown: str,
    srs_meta: dict,
    retry_comments: Optional[str] = None,
    previous_markdown: Optional[str] = None,
    progress_cb: Optional[Callable] = None
) -> tuple[str, dict]:
    """概要设计阶段"""
    config = get_stage_config("hld")
    return execute_stage(
        config=config,
        input_data={
            "srs_markdown": srs_markdown,
            "srs_meta_json": json.dumps(srs_meta, ensure_ascii=False)
        },
        retry_comments=retry_comments,
        previous_markdown=previous_markdown,
        progress_cb=progress_cb
    )


def design_lld(
    hld_markdown: str,
    hld_meta: dict,
    srs_markdown: str,
    retry_comments: Optional[str] = None,
    previous_markdown: Optional[str] = None,
    progress_cb: Optional[Callable] = None
) -> tuple[str, dict]:
    """详细设计阶段"""
    config = get_stage_config("lld")
    return execute_stage(
        config=config,
        input_data={
            "hld_markdown": hld_markdown,
            "hld_meta_json": json.dumps(hld_meta, ensure_ascii=False),
            "srs_markdown": srs_markdown
        },
        retry_comments=retry_comments,
        previous_markdown=previous_markdown,
        progress_cb=progress_cb
    )


def generate_testcases(
    srs_markdown: str,
    srs_meta: dict,
    retry_comments: Optional[str] = None,
    previous_markdown: Optional[str] = None,
    progress_cb: Optional[Callable] = None
) -> tuple[str, dict]:
    """测试用例生成阶段"""
    config = get_stage_config("testcase")
    return execute_stage(
        config=config,
        input_data={
            "srs_markdown": srs_markdown,
            "srs_meta_json": json.dumps(srs_meta, ensure_ascii=False)
        },
        retry_comments=retry_comments,
        previous_markdown=previous_markdown,
        progress_cb=progress_cb
    )


# 保留向后兼容的 save_to_normalized_tables 函数
def save_to_normalized_tables(session, artifact_id: int, stage: str, meta: dict):
    """将元数据保存到规范化表（从 stage_agents_v2 迁移）"""
    from db.repository import ArtifactRepository

    if stage == "requirement":
        if "functional_requirements" in meta:
            ArtifactRepository.save_functional_requirements(
                session, artifact_id, meta["functional_requirements"]
            )
        if "ambiguities" in meta:
            from db.models_v2 import Ambiguity
            for amb in meta["ambiguities"]:
                session.add(Ambiguity(
                    artifact_id=artifact_id,
                    amb_id=amb.get("id", ""),
                    description=amb.get("desc", ""),
                    severity=amb.get("severity", "中")
                ))
        if "risks" in meta:
            from db.models_v2 import Risk
            for risk in meta["risks"]:
                session.add(Risk(
                    artifact_id=artifact_id,
                    risk_id=risk.get("id", ""),
                    description=risk.get("desc", ""),
                    severity=risk.get("severity", "中")
                ))

    elif stage == "hld":
        if "modules" in meta:
            from db.models_v2 import Module
            for mod_name in meta["modules"]:
                session.add(Module(artifact_id=artifact_id, name=mod_name))
        if "tables" in meta:
            from db.models_v2 import DatabaseTable
            for tbl_name in meta["tables"]:
                session.add(DatabaseTable(artifact_id=artifact_id, table_name=tbl_name))
        if "apis" in meta:
            ArtifactRepository.save_apis(session, artifact_id, meta["apis"])

    elif stage == "lld":
        if "data_structures" in meta:
            from db.models_v2 import DataStructure
            for ds_name in meta["data_structures"]:
                session.add(DataStructure(artifact_id=artifact_id, name=ds_name, type="struct"))
        if "functions" in meta:
            from db.models_v2 import Function
            for func in meta["functions"]:
                session.add(Function(
                    artifact_id=artifact_id,
                    name=func.get("name", ""),
                    signature=func.get("sig", ""),
                    description=func.get("desc", "")
                ))

    elif stage == "testcase":
        if "testcases" in meta:
            ArtifactRepository.save_testcases(session, artifact_id, meta["testcases"])

    session.commit()
