"""项目与流水线相关 API。"""
import os
import uuid

from flask import Blueprint, jsonify, request

import config
from db.models import Document, PipelineLog, Project, SessionLocal, StageArtifact
from pipeline.nodes import STAGES, STAGE_TITLES
from services import pipeline_service

bp = Blueprint("projects", __name__, url_prefix="/api")

ALLOWED_EXT = {".docx", ".pdf", ".md", ".txt"}


@bp.get("/meta")
def meta():
    # 阶段定义为前后端单一数据源：后端 STAGES 不含 parse，前端展示需要含 parse 的完整流水线
    from core import web_search
    return jsonify({
        "mock": config.llm_mock_enabled(),
        "stages": ["parse", *STAGES],
        "stage_names": STAGE_TITLES,
        # 联网检索配置级开关：未配置 key 时前端不显示「联网」按钮
        "web_search_enabled": web_search.enabled(),
    })


@bp.get("/projects")
def list_projects():
    with SessionLocal() as session:
        projects = session.query(Project).order_by(Project.id.desc()).all()
        return jsonify([{
            "id": p.id, "name": p.name, "status": p.status,
            "current_stage": p.current_stage, "error": p.error,
            "created_at": p.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        } for p in projects])


@bp.post("/projects")
def create_project():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "项目名称不能为空"}), 400
    with SessionLocal() as session:
        p = Project(name=name)
        session.add(p)
        session.commit()
        return jsonify({"id": p.id, "name": p.name, "status": p.status})


@bp.post("/projects/<int:pid>/upload")
def upload(pid: int):
    if "file" not in request.files:
        return jsonify({"error": "未提供文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"不支持的文件类型 {ext}，支持 {sorted(ALLOWED_EXT)}"}), 400

    with SessionLocal() as session:
        if not session.get(Project, pid):
            return jsonify({"error": "项目不存在"}), 404
        # 同名文档去重：同项目已存在同名文件则直接拒绝，避免重复抽取
        exists = session.query(Document).filter_by(
            project_id=pid, filename=f.filename).count()
        if exists:
            return jsonify({"error": f"同名文档「{f.filename}」已存在，无需重复上传"}), 409
        stored = f"{uuid.uuid4().hex}{ext}"
        path = config.UPLOAD_DIR / stored
        f.save(str(path))
        ftype = ext.lstrip(".")
        ftype = "md" if ftype == "markdown" else ftype
        doc = Document(project_id=pid, filename=f.filename, stored_path=str(path),
                       file_type=ftype)
        session.add(doc)
        session.commit()
        return jsonify({"id": doc.id, "filename": doc.filename, "file_type": ftype})


@bp.delete("/projects/<int:pid>/documents/<int:doc_id>")
def delete_document(pid: int, doc_id: int):
    """删除项目内某个文档（流水线运行中禁止）。"""
    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        if p.status in ("running", "parsing"):
            return jsonify({"error": "流水线运行中，无法删除文档"}), 409
        doc = session.get(Document, doc_id)
        if not doc or doc.project_id != pid:
            return jsonify({"error": "文档不存在"}), 404
        from db.models import Chunk
        session.query(Chunk).filter_by(document_id=doc.id).delete()
        session.delete(doc)
        session.commit()
        return jsonify({"ok": True})


@bp.post("/projects/<int:pid>/start")
def start(pid: int):
    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        if p.status not in ("created", "failed"):
            return jsonify({"error": f"当前状态 {p.status} 不允许启动"}), 400
        has_doc = session.query(Document).filter_by(project_id=pid).count() > 0
        if not has_doc:
            return jsonify({"error": "请先上传需求文档"}), 400
    ok = pipeline_service.start_pipeline(pid)
    if not ok:
        return jsonify({"error": "流水线已在运行中"}), 409
    return jsonify({"ok": True})


@bp.post("/projects/<int:pid>/reset")
def reset(pid: int):
    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
    try:
        pipeline_service.reset_project(pid)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"ok": True})


@bp.post("/projects/<int:pid>/resume")
def resume(pid: int):
    """断点续跑：失败后从最后成功的阶段继续，不重跑已完成部分。"""
    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        if p.status != "failed":
            return jsonify({"error": f"当前状态 {p.status} 无需断点续跑"}), 400
    if not pipeline_service.resumable_stage(pid):
        return jsonify({"error": "无可续跑的检查点，请使用重置重跑"}), 409
    ok = pipeline_service.resume_pipeline(pid)
    if not ok:
        return jsonify({"error": "流水线已在运行中"}), 409
    return jsonify({"ok": True})


@bp.post("/projects/<int:pid>/cancel")
def cancel(pid: int):
    """请求取消运行中的流水线（在当前并发抽取批次结束后停止）。"""
    from core import cancel as cancel_mod
    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        if p.status not in ("running", "parsing"):
            return jsonify({"error": f"当前状态 {p.status} 无需取消"}), 400
    cancel_mod.request_cancel(pid)
    return jsonify({"ok": True, "message": "取消请求已发送，流水线停止后可重置重跑"})


@bp.get("/projects/<int:pid>/status")
def status(pid: int):
    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        arts = {}
        for stage in ["parse", *STAGES]:
            art = (session.query(StageArtifact)
                   .filter_by(project_id=pid, stage=stage)
                   .order_by(StageArtifact.version.desc()).first())
            if art:
                arts[stage] = {"artifact_id": art.id, "version": art.version,
                               "title": art.title, "status": art.status}
        docs = [{"id": d.id, "filename": d.filename, "status": d.status}
                for d in session.query(Document).filter_by(project_id=pid)]
        # 失败时检查是否存在可续跑的检查点（失败前已有阶段成功完成）
        resumable = False
        if p.status == "failed":
            resumable = bool(pipeline_service.resumable_stage(pid))
        return jsonify({
            "id": p.id, "name": p.name, "status": p.status,
            "current_stage": p.current_stage, "error": p.error,
            "artifacts": arts, "documents": docs, "resumable": resumable,
        })


@bp.get("/projects/<int:pid>/stages/<stage>/artifact")
def artifact(pid: int, stage: str):
    with SessionLocal() as session:
        art = (session.query(StageArtifact)
               .filter_by(project_id=pid, stage=stage)
               .order_by(StageArtifact.version.desc()).first())
        if not art:
            return jsonify({"error": f"阶段 {stage} 暂无产物"}), 404
        return jsonify({
            "artifact_id": art.id, "stage": art.stage, "version": art.version,
            "title": art.title, "markdown": art.markdown,
            "meta": art.meta_json, "status": art.status,
        })


@bp.post("/projects/<int:pid>/stages/<stage>/review")
def review(pid: int, stage: str):
    data = request.json or {}
    approved = bool(data.get("approved"))
    comments = (data.get("comments") or "").strip()
    if not approved and not comments:
        return jsonify({"error": "驳回时必须填写评审意见"}), 400
    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        if p.status != "waiting_review" or p.current_stage != stage:
            return jsonify({"error": f"当前阶段 {p.current_stage}（状态 {p.status}）不在等待评审"}), 409
    ok = pipeline_service.submit_review(pid, approved, comments)
    if not ok:
        return jsonify({"error": "流水线正忙，请稍后重试"}), 409
    return jsonify({"ok": True})


@bp.get("/projects/<int:pid>/logs")
def logs(pid: int):
    with SessionLocal() as session:
        rows = (session.query(PipelineLog)
                .filter_by(project_id=pid)
                .order_by(PipelineLog.id.asc()).all())
        return jsonify([{
            "time": r.created_at.strftime("%H:%M:%S"), "level": r.level,
            "stage": r.stage, "message": r.message,
        } for r in rows])


STAGE_LABELS = {"parse": STAGE_TITLES["parse"], **{s: STAGE_TITLES[s] for s in STAGES}}
