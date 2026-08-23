"""重构后的流水线服务：使用统一状态管理器。

主要改进：
1. 使用 StateManager 统一管理状态
2. 简化运行标记逻辑（不再需要独立的 _running 集合）
3. 更清晰的错误处理
4. 去除复杂的状态恢复逻辑（状态管理器已处理）
"""
import threading
import traceback

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import config
from core.state_manager import ProjectStatus, StageType, get_state_manager
from db.models import PipelineLog, SessionLocal
from pipeline.graph import build_graph


def _log(project_id: int, message: str, level: str = "INFO", stage: StageType = None):
    """记录日志"""
    with SessionLocal() as session:
        session.add(PipelineLog(
            project_id=project_id,
            level=level,
            stage=stage.value if stage else None,
            message=message
        ))
        session.commit()


def _run_graph(project_id: int, invoke_input):
    """在后台线程执行图（带状态管理）"""
    state_mgr = get_state_manager()

    try:
        with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB)) as cp:
            graph = build_graph(cp)
            cfg = {"configurable": {"thread_id": f"proj_{project_id}"}}

            # 执行图
            graph.invoke(invoke_input, config=cfg)

            # 检查是否完成
            snap = graph.get_state(cfg)

        with SessionLocal() as session:
            if snap.next:
                # 停在 interrupt（评审门）
                # 注意：agent节点已经把状态设为 WAITING_REVIEW
                _log(project_id, "流水线暂停，等待人工评审")
            else:
                # 到达 END，全部完成
                state_mgr.transition(
                    session,
                    project_id,
                    ProjectStatus.COMPLETED,
                    log_message="全部阶段评审通过，流水线完成"
                )

    except Exception as e:
        error_msg = f"{e}\n{traceback.format_exc()}"
        _log(project_id, f"流水线异常终止: {e}", "ERROR")

        with SessionLocal() as session:
            try:
                state_mgr.transition(
                    session,
                    project_id,
                    ProjectStatus.FAILED,
                    error=str(e)
                )
            except Exception as trans_err:
                # 状态转换失败，直接记录日志
                _log(project_id, f"状态转换失败: {trans_err}", "ERROR")

    finally:
        # 清除运行标记
        state_mgr.unmark_running(project_id)


def _clear_checkpoints(project_id: int):
    """删除项目的全部 LangGraph 检查点"""
    import sqlite3
    conn = sqlite3.connect(str(config.CHECKPOINT_DB))
    thread_id = f"proj_{project_id}"
    try:
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs", "checkpoint_migrations"):
            try:
                conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


def start_pipeline(project_id: int) -> tuple[bool, str]:
    """启动新流水线

    Returns:
        (success: bool, message: str)
    """
    from core import cancel as cancel_mod

    state_mgr = get_state_manager()

    with SessionLocal() as session:
        # 检查是否可以启动
        can_start, reason = state_mgr.can_start(session, project_id)
        if not can_start:
            return False, reason

        # 标记为运行中
        if not state_mgr.mark_running(project_id):
            return False, "项目正在运行中"

        try:
            # 状态转换：READY/FAILED -> PARSING
            state_mgr.transition(
                session,
                project_id,
                ProjectStatus.PARSING,
                stage=StageType.PARSE,
                log_message="启动流水线"
            )

            # 清理旧检查点和取消标志
            cancel_mod.clear(project_id)
            _clear_checkpoints(project_id)

            # 启动后台线程
            threading.Thread(
                target=_run_graph,
                args=(project_id, {"project_id": project_id}),
                daemon=True,
                name=f"pipeline-{project_id}"
            ).start()

            return True, "流水线已启动"

        except Exception as e:
            state_mgr.unmark_running(project_id)
            return False, f"启动失败: {e}"


def submit_review(project_id: int, approved: bool, comments: str = "") -> tuple[bool, str]:
    """提交评审决策，恢复暂停的流水线

    Returns:
        (success: bool, message: str)
    """
    state_mgr = get_state_manager()

    with SessionLocal() as session:
        state = state_mgr.load_state(session, project_id)

        # 验证状态
        if state.status != ProjectStatus.WAITING_REVIEW:
            return False, f"当前状态 {state.status.value} 不在等待评审"

        # 标记为运行中
        if not state_mgr.mark_running(project_id):
            return False, "项目正在运行中"

        try:
            # 状态转换：WAITING_REVIEW -> RUNNING
            state_mgr.transition(
                session,
                project_id,
                ProjectStatus.RUNNING,
                stage=state.current_stage,
                log_message=f"评审{'通过' if approved else '驳回'}，继续执行"
            )

            # 启动后台线程恢复执行
            threading.Thread(
                target=_run_graph,
                args=(project_id, Command(resume={"approved": approved, "comments": comments})),
                daemon=True,
                name=f"pipeline-{project_id}-resume"
            ).start()

            return True, "评审已提交"

        except Exception as e:
            state_mgr.unmark_running(project_id)
            return False, f"提交失败: {e}"


def resume_pipeline(project_id: int) -> tuple[bool, str]:
    """断点续跑（失败后从最后成功的节点继续）

    Returns:
        (success: bool, message: str)
    """
    from core import cancel as cancel_mod

    state_mgr = get_state_manager()

    with SessionLocal() as session:
        state = state_mgr.load_state(session, project_id)

        # 验证状态
        if state.status != ProjectStatus.FAILED:
            return False, f"当前状态 {state.status.value} 无需断点续跑"

        # 检查是否有可恢复的检查点
        resumable_stage = _get_resumable_stage(project_id)
        if not resumable_stage:
            return False, "无可续跑的检查点，请重置后重新启动"

        # 标记为运行中
        if not state_mgr.mark_running(project_id):
            return False, "项目正在运行中"

        try:
            # 状态转换：FAILED -> PARSING/RUNNING（根据断点位置）
            next_status = ProjectStatus.PARSING if resumable_stage == "parse_input" else ProjectStatus.RUNNING
            state_mgr.transition(
                session,
                project_id,
                next_status,
                log_message=f"断点续跑，从 {resumable_stage} 继续"
            )

            cancel_mod.clear(project_id)

            # 启动后台线程
            threading.Thread(
                target=_run_graph,
                args=(project_id, None),
                daemon=True,
                name=f"pipeline-{project_id}-resume"
            ).start()

            return True, "断点续跑已启动"

        except Exception as e:
            state_mgr.unmark_running(project_id)
            return False, f"续跑失败: {e}"


def _get_resumable_stage(project_id: int) -> str | None:
    """获取可续跑的节点名称"""
    try:
        with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB)) as cp:
            graph = build_graph(cp)
            cfg = {"configurable": {"thread_id": f"proj_{project_id}"}}
            snap = graph.get_state(cfg)

        if snap and snap.next:
            for task in snap.tasks:
                if getattr(task, "interrupts", None):
                    return None
            return snap.next[0]

        return None
    except Exception:
        return None


def reset_project(project_id: int) -> tuple[bool, str]:
    """重置项目"""
    import sqlite3
    from db.models import Chunk, Document, PipelineLog, Review, StageArtifact

    state_mgr = get_state_manager()

    if state_mgr.is_running(project_id):
        return False, "流水线正在运行中，无法重置"

    try:
        _clear_checkpoints(project_id)

        with SessionLocal() as session:
            docs = session.query(Document).filter_by(project_id=project_id).all()
            doc_ids = [d.id for d in docs]
            if doc_ids:
                session.query(Chunk).filter(Chunk.document_id.in_(doc_ids)).delete(
                    synchronize_session=False
                )

            arts = session.query(StageArtifact).filter_by(project_id=project_id).all()
            art_ids = [a.id for a in arts]
            if art_ids:
                session.query(Review).filter(Review.artifact_id.in_(art_ids)).delete(
                    synchronize_session=False
                )

            session.query(StageArtifact).filter_by(project_id=project_id).delete()
            session.query(PipelineLog).filter_by(project_id=project_id).delete()

            for d in docs:
                d.status = "uploaded"
                d.parse_error = None

            session.commit()

            state_mgr.reset(session, project_id)
            _log(project_id, "项目已重置，可重新启动流水线")

        return True, "项目已重置"

    except Exception as e:
        return False, f"重置失败: {e}"


def auto_resume_orphans() -> list[int]:
    """进程重启后自动恢复孤儿项目"""
    from db.models import Project

    state_mgr = get_state_manager()
    resumed = []

    with SessionLocal() as session:
        stuck = session.query(Project).filter(
            Project.status.in_(("running", "parsing", "waiting_review"))
        ).all()

    for proj in stuck:
        pid = proj.id
        try:
            with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB)) as cp:
                graph = build_graph(cp)
                cfg = {"configurable": {"thread_id": f"proj_{pid}"}}
                snap = graph.get_state(cfg)

            if not snap or not snap.next:
                with SessionLocal() as session:
                    state_mgr.transition(
                        session, pid, ProjectStatus.FAILED,
                        error="进程重启时流水线正在执行，请重新启动"
                    )
                continue

            is_at_interrupt = False
            for task in snap.tasks:
                if getattr(task, "interrupts", None):
                    is_at_interrupt = True
                    break

            with SessionLocal() as session:
                if is_at_interrupt:
                    state_mgr.transition(
                        session, pid, ProjectStatus.WAITING_REVIEW,
                        log_message="进程重启检测到等待评审状态，已恢复"
                    )
                else:
                    success, msg = resume_pipeline(pid)
                    if success:
                        resumed.append(pid)
                        _log(pid, f"检测到进程重启中断，已自动断点续跑")

        except Exception as e:
            with SessionLocal() as session:
                try:
                    state_mgr.transition(
                        session, pid, ProjectStatus.FAILED,
                        error=f"状态恢复失败: {e}"
                    )
                except Exception:
                    pass

    return resumed
