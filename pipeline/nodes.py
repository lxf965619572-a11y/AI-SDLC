"""流水线节点：解析抽取、四个智能体节点、评审门控节点。
每个节点自管 DB session；产物写入 stage_artifacts（驳回重跑时版本+1）。"""
import json
import traceback

from db.models import (Chunk, Document, PipelineLog, Project, Review,
                       SessionLocal, StageArtifact)
from pipeline.state import PipelineState

# 阶段顺序与展示名
STAGES = ["requirement", "hld", "lld", "testcase"]
STAGE_TITLES = {
    "parse": "结构化原始数据",
    "requirement": "软件需求规格说明书",
    "hld": "概要设计说明书",
    "lld": "详细设计说明书",
    "testcase": "测试用例设计",
}


def log(session, project_id: int, message: str, stage: str | None = None,
        level: str = "INFO"):
    session.add(PipelineLog(project_id=project_id, level=level, stage=stage,
                            message=message))
    session.commit()


def _set_status(session, project_id: int, status: str, stage: str | None = None):
    proj = session.get(Project, project_id)
    if proj:
        proj.status = status
        if stage:
            proj.current_stage = stage
        session.commit()


def _save_artifact(session, project_id: int, stage: str, markdown: str,
                   meta: dict | None, approved_status: bool = False) -> int:
    """保存产物；同阶段已有产物则版本+1。返回 artifact_id。"""
    prev = session.query(StageArtifact).filter_by(
        project_id=project_id, stage=stage).count()
    art = StageArtifact(
        project_id=project_id, stage=stage, version=prev + 1,
        title=STAGE_TITLES.get(stage, stage), markdown=markdown,
        meta_json=meta, status="approved" if approved_status else "pending_review",
    )
    session.add(art)
    session.commit()
    return art.id


def _get_artifact(session, artifact_id: int) -> StageArtifact | None:
    return session.get(StageArtifact, artifact_id)


# ---------------- 阶段1：输入解析与抽取 ----------------
def parse_input(state: PipelineState) -> dict:
    from parsing.chunker import chunk_paragraphs
    from parsing.doc_parser import parse_document
    from parsing.extractor import extract_structured, render_markdown

    project_id = state["project_id"]
    with SessionLocal() as session:
        _set_status(session, project_id, "parsing", "parse")
        log(session, project_id, "开始解析上传文档", "parse")
        docs = session.query(Document).filter_by(project_id=project_id).all()
        if not docs:
            raise RuntimeError("项目没有上传任何文档")

        # 重跑保护：清理本项目旧分块，避免重复累积
        for doc in docs:
            session.query(Chunk).filter_by(document_id=doc.id).delete()
        session.commit()

        all_chunks = []
        for doc in docs:
            try:
                paras = parse_document(doc.stored_path, doc.file_type)
                chunks = chunk_paragraphs(paras)
                for c in chunks:
                    session.add(Chunk(document_id=doc.id, seq=c["seq"],
                                      heading=c["heading"], content=c["content"]))
                doc.status = "parsed"
                session.commit()
                all_chunks.extend(chunks)
                log(session, project_id, f"文档 {doc.filename} 解析完成，{len(chunks)} 个分块", "parse")
            except Exception as e:
                doc.status = "failed"
                doc.parse_error = str(e)
                session.commit()
                log(session, project_id, f"文档 {doc.filename} 解析失败: {e}", "parse", "ERROR")

        if not all_chunks:
            raise RuntimeError("所有文档解析失败，无可抽取内容")

        total = len(all_chunks)
        from parsing.extractor import BATCH_SIZE
        n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        log(session, project_id,
            f"开始 LLM 抽取，共 {total} 个分块，分 {n_batches} 批并发抽取（每批 {BATCH_SIZE} 块）", "parse")
        last_pct = {"v": 0}
        import time as _time
        t0 = _time.time()
        live = {"done": 0}  # 真实 LLM 批次数（不含缓存命中），用于 ETA

        def progress_cb(done: int, tot: int, cached: bool = False):
            if not cached:
                live["done"] += 1
            pct = done * 100 // tot
            # 每推进 5% 或首批完成记一条进度日志（含预计剩余时间）
            if done == 1 or pct >= last_pct["v"] + 5 or done == tot:
                last_pct["v"] = pct
                if not cached and live["done"] > 0:
                    eta_s = (_time.time() - t0) / live["done"] * (tot - done)
                    eta = f"，预计还需约 {int(eta_s // 60)} 分 {int(eta_s % 60)} 秒"
                else:
                    eta = ""
                hit = "（缓存命中）" if cached else ""
                log(session, project_id,
                    f"LLM 抽取进度：第 {done}/{tot} 批完成（{pct}%）{hit}{eta}", "parse")

        def reduce_cb(message: str):
            log(session, project_id, f"合并去重：{message}", "parse")

        structured = extract_structured(all_chunks, progress_cb=progress_cb,
                                        reduce_cb=reduce_cb, project_id=project_id)
        md = render_markdown(structured, [d.filename for d in docs if d.status == "parsed"])
        art_id = _save_artifact(session, project_id, "parse", md, structured,
                                approved_status=True)  # 解析阶段自动通过，无需评审
        log(session, project_id, "结构化抽取完成", "parse")
        return {"structured": structured,
                "artifacts": {"parse": {"id": art_id, "version": 1}}}


# ---------------- 阶段2-5：智能体节点工厂 ----------------
def _get_approved(session, project_id: int, stage: str) -> StageArtifact | None:
    """取该阶段最新一版产物。"""
    return (session.query(StageArtifact)
            .filter_by(project_id=project_id, stage=stage)
            .order_by(StageArtifact.version.desc()).first())


def make_agent(stage: str):
    def agent_node(state: PipelineState) -> dict:
        from agents import stage_agents

        project_id = state["project_id"]
        retry_comments = state.get("retry_comments")
        with SessionLocal() as session:
            _set_status(session, project_id, "running", stage)
            prev_art = _get_approved(session, project_id, stage)
            prev_md = prev_art.markdown if (retry_comments and prev_art) else None
            log(session, project_id,
                f"{'带评审意见修订' if retry_comments else '生成'}「{STAGE_TITLES[stage]}」", stage)

            # 流式生成进度：按累计字符数每推进 ~2000 字记一条日志
            last_chars = {"v": 0}

            def gen_cb(chars: int):
                if chars - last_chars["v"] >= 2000:
                    last_chars["v"] = chars
                    log(session, project_id,
                        f"「{STAGE_TITLES[stage]}」生成中，已输出约 {chars} 字符", stage)

            try:
                if stage == "requirement":
                    md, meta = stage_agents.analyze_requirements(
                        json.dumps(state["structured"], ensure_ascii=False),
                        retry_comments, prev_md, progress_cb=gen_cb)
                elif stage == "hld":
                    srs = _get_approved(session, project_id, "requirement")
                    md, meta = stage_agents.design_hld(
                        srs.markdown, srs.meta_json or {}, retry_comments, prev_md,
                        progress_cb=gen_cb)
                elif stage == "lld":
                    hld = _get_approved(session, project_id, "hld")
                    srs = _get_approved(session, project_id, "requirement")
                    md, meta = stage_agents.design_lld(
                        hld.markdown, hld.meta_json or {}, srs.markdown,
                        retry_comments, prev_md, progress_cb=gen_cb)
                elif stage == "testcase":
                    srs = _get_approved(session, project_id, "requirement")
                    md, meta = stage_agents.generate_testcases(
                        srs.markdown, srs.meta_json or {}, retry_comments, prev_md,
                        progress_cb=gen_cb)
                else:
                    raise ValueError(f"未知阶段: {stage}")
            except Exception as e:
                log(session, project_id, f"智能体执行失败: {e}\n{traceback.format_exc()}",
                    stage, "ERROR")
                raise

            art_id = _save_artifact(session, project_id, stage, md, meta)
            version = prev_art.version + 1 if prev_art else 1
            log(session, project_id, f"「{STAGE_TITLES[stage]}」v{version} 生成完成", stage)
            _set_status(session, project_id, "waiting_review", stage)
            log(session, project_id, f"「{STAGE_TITLES[stage]}」等待人工评审", stage)

            update = {
                "artifacts": {stage: {"id": art_id, "version": version}},
                "retry_comments": None,
                "current_stage": stage,
            }
            if stage == "requirement":
                update["srs_meta"] = meta
            elif stage == "hld":
                update["hld_meta"] = meta
            return update

    return agent_node


# ---------------- 评审门控节点 ----------------
def make_gate(stage: str):
    def gate_node(state: PipelineState) -> dict:
        from langgraph.types import interrupt

        project_id = state["project_id"]
        art = state["artifacts"][stage]

        # 暂停，等待人工评审；resume 时收到 {"approved": bool, "comments": str}
        # 注意：interrupt 之前的代码会在 resume 时重放，故 DB 写入全部放在其后
        decision = interrupt({
            "project_id": project_id, "stage": stage,
            "artifact_id": art["id"], "version": art["version"],
        })

        approved = bool(decision.get("approved"))
        comments = decision.get("comments") or ""
        with SessionLocal() as session:
            a = _get_artifact(session, art["id"])
            if a:
                a.status = "approved" if approved else "rejected"
                session.commit()
            session.add(Review(project_id=project_id, artifact_id=art["id"],
                               approved=approved, comments=comments))
            session.commit()
            log(session, project_id,
                f"评审结果：{'通过' if approved else '驳回'}"
                + (f"（意见：{comments[:200]}）" if comments else ""), stage)
            if approved:
                _set_status(session, project_id, "running", stage)
        return {"retry_comments": comments if not approved else None}

    return gate_node


# ---------------- 路由：通过→下一阶段，驳回→回环重跑 ----------------
def next_stage_or_end(stage: str) -> str:
    idx = STAGES.index(stage)
    return f"agent_{STAGES[idx + 1]}" if idx + 1 < len(STAGES) else "__end__"


def make_route(stage: str):
    def route(state: PipelineState) -> str:
        return "redo" if state.get("retry_comments") else "next"
    return route
