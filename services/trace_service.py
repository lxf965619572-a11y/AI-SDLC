"""需求追溯服务：从各阶段最新产物的元数据拼出「需求 → 设计 → 用例」链路。

计算逻辑全在 core.trace（纯函数、可离线单测），这里只负责取数与补充展示信息。
"""
from core import trace
from db.models import SessionLocal, StageArtifact


def _latest(session, project_id: int, stage: str):
    art = (session.query(StageArtifact)
           .filter_by(project_id=project_id, stage=stage)
           .order_by(StageArtifact.version.desc()).first())
    if not art:
        return {}, None
    return (art.meta_json or {}), art.version


def build_project_matrix(project_id: int) -> dict:
    """返回 {rows, summary, versions, sources}；rows 每条需求一行。"""
    with SessionLocal() as session:
        srs, srs_v = _latest(session, project_id, "requirement")
        hld, hld_v = _latest(session, project_id, "hld")
        lld, lld_v = _latest(session, project_id, "lld")
        tc, tc_v = _latest(session, project_id, "testcase")
        parse_meta, _ = _latest(session, project_id, "parse")

    data = trace.build_matrix(srs, hld, lld, tc)
    data["versions"] = {"requirement": srs_v, "hld": hld_v,
                        "lld": lld_v, "testcase": tc_v}

    # 素材索引：编号 → 名称/出处，让前端能把 FR 的来源编号翻译成人能读的内容
    sources: dict[str, dict] = {}
    for key, prefix in trace.SRC_PREFIXES.items():
        for it in parse_meta.get(key) or []:
            if isinstance(it, dict) and it.get("id"):
                sources[it["id"]] = {"kind": prefix,
                                     "name": it.get("name", ""),
                                     "chunks": it.get("source") or []}
    data["sources"] = sources
    for row in data["rows"]:
        row["source_names"] = [(sources.get(s) or {}).get("name") or s
                               for s in row["sources"]]
    return data
