"""流水线执行服务：后台线程运行图、评审决策恢复、跨进程状态恢复。"""
import threading
import traceback

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import config
from core import stream_bus
from db.models import PipelineLog, Project, SessionLocal
from pipeline.graph import build_graph

_lock = threading.Lock()
_running: set[int] = set()


def _thread_id(project_id: int) -> str:
    return f"proj_{project_id}"


def _config(project_id: int) -> dict:
    return {"configurable": {"thread_id": _thread_id(project_id)}}


def _set_status(project_id: int, status: str, error: str | None = None):
    with SessionLocal() as session:
        proj = session.get(Project, project_id)
        if proj:
            proj.status = status
            if error is not None:
                proj.error = error
            elif status in ("running", "parsing"):
                proj.error = None  # 进入运行态时清除旧错误信息
            session.commit()


def _log(project_id: int, message: str, level: str = "INFO"):
    with SessionLocal() as session:
        session.add(PipelineLog(project_id=project_id, level=level, message=message))
        session.commit()


def _run_graph(project_id: int, invoke_input):
    """在后台线程执行 graph.invoke；结束后更新运行集合。
    注意：invoke 在到达 END 或遇到 interrupt（评审门）时都会返回，
    需用 get_state().next 区分二者。"""
    # 本轮执行的实时事件通道：智能体输出的增量经此推给前端 SSE
    stream_bus.open_channel(project_id)
    reason = "stopped"
    try:
        with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB)) as cp:
            graph = build_graph(cp)
            graph.invoke(invoke_input, config=_config(project_id))
            snap = graph.get_state(_config(project_id))
        if snap.next:
            # 停在 interrupt 评审门：agent 节点已把状态置为 waiting_review
            reason = "waiting_review"
            _log(project_id, "流水线暂停，等待人工评审")
        else:
            reason = "completed"
            _set_status(project_id, "completed")
            _log(project_id, "全部阶段评审通过，流水线完成")
    except Exception as e:
        reason = "failed"
        _log(project_id, f"流水线异常终止: {e}\n{traceback.format_exc()}", "ERROR")
        _set_status(project_id, "failed", str(e))
    finally:
        # 关通道会先给订阅端发 run_end，再让 SSE 生成器正常收尾
        stream_bus.close_channel(project_id, reason)
        with _lock:
            _running.discard(project_id)


def _mark_running(project_id: int) -> bool:
    with _lock:
        if project_id in _running:
            return False
        _running.add(project_id)
        return True


def _clear_checkpoints(project_id: int):
    """删除该项目的全部 LangGraph 检查点，保证从 START 全新执行。"""
    import sqlite3
    conn = sqlite3.connect(str(config.CHECKPOINT_DB))
    try:
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs",
                      "checkpoint_migrations"):
            try:
                conn.execute(f"DELETE FROM {table} WHERE thread_id = ?",
                             (_thread_id(project_id),))
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


def start_pipeline(project_id: int) -> bool:
    """启动新流水线。若已在运行返回 False。"""
    from core import cancel as cancel_mod

    if not _mark_running(project_id):
        return False
    cancel_mod.clear(project_id)  # 清理残留取消标志，避免新运行被立即取消
    _clear_checkpoints(project_id)
    _set_status(project_id, "running")
    threading.Thread(
        target=_run_graph, args=(project_id, {"project_id": project_id}),
        daemon=True).start()
    return True


def submit_review(project_id: int, approved: bool, comments: str = "") -> bool:
    """提交评审决策，恢复被 interrupt 暂停的流水线。"""
    if not _mark_running(project_id):
        return False
    _set_status(project_id, "running")
    threading.Thread(
        target=_run_graph,
        args=(project_id, Command(resume={"approved": approved, "comments": comments})),
        daemon=True).start()
    return True


def resumable_stage(project_id: int) -> str | None:
    """检查检查点中是否有可续跑的位置（失败后停在某节点）。
    返回下一个待执行节点名；停在 interrupt（等待评审）或无检查点时返回 None。"""
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB)) as cp:
        graph = build_graph(cp)
        snap = graph.get_state(_config(project_id))
    if snap and snap.next:
        for task in snap.tasks:
            if getattr(task, "interrupts", None):
                return None  # 停在评审门，由 submit_review 处理
        return snap.next[0]
    return None


def auto_resume_orphans() -> list[int]:
    """进程重启后：对卡在 running/parsing 且检查点可续跑的孤儿项目自动断点续跑。
    解析阶段的抽取缓存保证已完成批次不重复调用 LLM。返回成功续跑的项目 id 列表。"""
    resumed = []
    with SessionLocal() as session:
        stuck = session.query(Project).filter(
            Project.status.in_(("running", "parsing"))).all()
        ids = [p.id for p in stuck]
    for pid in ids:
        try:
            stage = resumable_stage(pid)
            if stage and resume_pipeline(pid):
                _log(pid, f"检测到进程重启中断于 {stage} 阶段，已自动断点续跑（缓存命中批次不重跑）")
                resumed.append(pid)
        except Exception:
            pass
    return resumed


def resume_pipeline(project_id: int) -> bool:
    """断点续跑：不清检查点，从最后成功的节点之后继续执行（invoke(None)）。"""
    from core import cancel as cancel_mod

    if not _mark_running(project_id):
        return False
    cancel_mod.clear(project_id)
    _set_status(project_id, "running")
    _log(project_id, "断点续跑：从最后成功的阶段继续，不重跑已完成部分")
    threading.Thread(
        target=_run_graph, args=(project_id, None),
        daemon=True).start()
    return True


def detect_interrupted_stage(project_id: int) -> dict | None:
    """检查图中当前是否停在某个 interrupt（等待评审）。返回 interrupt payload 或 None。"""
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB)) as cp:
        graph = build_graph(cp)
        snap = graph.get_state(_config(project_id))
    if snap and snap.next and snap.tasks:
        for task in snap.tasks:
            if getattr(task, "interrupts", None):
                return task.interrupts[0].value
    return None


def try_resume_orphan(project_id: int):
    """进程重启后，若发现流水线停在评审门（interrupt），保持 waiting_review 状态等待人工操作。"""
    payload = detect_interrupted_stage(project_id)
    if payload:
        _set_status(project_id, "waiting_review")


def reset_project(project_id: int):
    """重置项目：清空检查点、产物、分块、评审与日志，回到待启动状态。"""
    import sqlite3

    from db.models import Chunk, Document, PipelineLog, Review, StageArtifact

    with _lock:
        if project_id in _running:
            raise RuntimeError("流水线正在运行中，无法重置")

    # 清理 LangGraph 检查点（各版本表结构不同，逐表尝试）
    conn = sqlite3.connect(str(config.CHECKPOINT_DB))
    try:
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs",
                      "checkpoint_migrations"):
            try:
                conn.execute(f"DELETE FROM {table} WHERE thread_id = ?",
                             (_thread_id(project_id),))
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()

    with SessionLocal() as session:
        docs = session.query(Document).filter_by(project_id=project_id).all()
        doc_ids = [d.id for d in docs]
        if doc_ids:
            session.query(Chunk).filter(Chunk.document_id.in_(doc_ids)).delete(
                synchronize_session=False)
        arts = session.query(StageArtifact).filter_by(project_id=project_id).all()
        art_ids = [a.id for a in arts]
        if art_ids:
            session.query(Review).filter(Review.artifact_id.in_(art_ids)).delete(
                synchronize_session=False)
        session.query(StageArtifact).filter_by(project_id=project_id).delete()
        session.query(PipelineLog).filter_by(project_id=project_id).delete()
        for d in docs:
            d.status = "uploaded"
            d.parse_error = None
        proj = session.get(Project, project_id)
        if proj:
            proj.status = "created"
            proj.current_stage = None
            proj.error = None
        session.commit()
    _log(project_id, "项目已重置，可重新启动流水线")


def delete_project(project_id: int) -> dict:
    """彻底删除项目：库内记录 + LangGraph 检查点 + 磁盘产物（上传件、导出件、验证证据）。

    与 reset_project 的区别：reset 保留项目与已上传文档、只清进度；delete 连项目
    本身一起抹掉，侧栏不再出现，且不可恢复。

    顺序是「先库后盘」：库里删干净了再删文件，反过来的话一旦删库失败就会留下
    一个「记录还在、产物没了」的半截项目。文件删失败不影响删除成功——那些目录
    已经没有任何记录指向它们，最坏结果只是占点磁盘，会在返回值里如实报出来。

    运行中禁止删除：后台线程还在写库写文件，边删边写必然留下脏数据。
    """
    from core import cancel as cancel_mod
    from db.models import Chunk, Document, Review, StageArtifact
    from services import storage

    with _lock:
        if project_id in _running:
            raise RuntimeError("流水线正在运行中，无法删除；请先取消并等它停下来")

    with SessionLocal() as session:
        proj = session.get(Project, project_id)
        if proj is None:
            raise LookupError("项目不存在")
        if proj.status in ("running", "parsing"):
            raise RuntimeError("流水线运行中，无法删除；请先取消并等它停下来")
        name = proj.name
        docs = session.query(Document).filter_by(project_id=project_id).all()
        uploads = [d.stored_path for d in docs if d.stored_path]
        counts = {
            "documents": len(docs),
            "artifacts": session.query(StageArtifact)
            .filter_by(project_id=project_id).count(),
        }
        doc_ids = [d.id for d in docs]
        if doc_ids:
            session.query(Chunk).filter(Chunk.document_id.in_(doc_ids)).delete(
                synchronize_session=False)
        session.query(Review).filter_by(project_id=project_id).delete(
            synchronize_session=False)
        session.query(StageArtifact).filter_by(project_id=project_id).delete(
            synchronize_session=False)
        session.query(PipelineLog).filter_by(project_id=project_id).delete(
            synchronize_session=False)
        session.query(Document).filter_by(project_id=project_id).delete(
            synchronize_session=False)
        session.delete(proj)
        session.commit()

    # 库外状态：检查点（否则同名 thread_id 复用会让新项目继承旧图状态）、
    # 取消标志、SSE 通道（不关会一直挂在注册表里）。
    _clear_checkpoints(project_id)
    cancel_mod.clear(project_id)
    stream_bus.close_channel(project_id, "deleted")

    removed_uploads = sum(1 for p in uploads if storage.remove_upload(p))
    dirs = storage.remove_project_dirs(project_id)
    return {"id": project_id, "name": name, **counts,
            "uploads_removed": removed_uploads, "uploads_total": len(uploads),
            "outputs_removed": dirs["outputs"], "evidence_removed": dirs["evidence"],
            "remote_workdir": storage.remote_workdir_hint(project_id)}
