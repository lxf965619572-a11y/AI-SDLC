"""统一状态管理：单一真相源模式。

设计原则：
1. ProjectState 是唯一的状态权威源
2. 所有状态变更通过 StateManager 进行并记录事件
3. DB 中的 Project.status 是状态的快照（派生视图）
4. LangGraph checkpoint 只用于流程恢复，不参与状态判断
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy.orm import Session

from db.models import PipelineLog, Project


class ProjectStatus(Enum):
    """项目状态枚举（有限状态机）"""
    CREATED = "created"              # 已创建，待上传文档
    READY = "ready"                  # 已上传文档，可启动
    PARSING = "parsing"              # 解析中
    RUNNING = "running"              # 智能体执行中
    WAITING_REVIEW = "waiting_review"  # 等待人工评审
    COMPLETED = "completed"          # 全部完成
    FAILED = "failed"                # 失败
    CANCELLED = "cancelled"          # 已取消


class StageType(Enum):
    """阶段类型"""
    PARSE = "parse"
    REQUIREMENT = "requirement"
    HLD = "hld"
    LLD = "lld"
    TESTCASE = "testcase"


@dataclass
class ProjectState:
    """项目运行时状态（完整的状态表示）"""
    project_id: int
    status: ProjectStatus
    current_stage: Optional[StageType]
    error: Optional[str]
    # 运行时标记（不持久化到DB）
    is_running_in_memory: bool = False  # 是否在当前进程的线程中运行
    last_update: datetime = None

    def __post_init__(self):
        if self.last_update is None:
            self.last_update = datetime.now()


class StateTransitionError(Exception):
    """非法状态转换异常"""
    pass


class StateManager:
    """状态管理器：管理所有项目的状态转换和持久化"""

    # 合法的状态转换矩阵（有限状态机）
    VALID_TRANSITIONS = {
        ProjectStatus.CREATED: {ProjectStatus.READY},
        ProjectStatus.READY: {ProjectStatus.PARSING},
        ProjectStatus.PARSING: {ProjectStatus.RUNNING, ProjectStatus.FAILED, ProjectStatus.CANCELLED},
        ProjectStatus.RUNNING: {
            ProjectStatus.WAITING_REVIEW,
            ProjectStatus.COMPLETED,
            ProjectStatus.FAILED,
            ProjectStatus.CANCELLED
        },
        ProjectStatus.WAITING_REVIEW: {ProjectStatus.RUNNING, ProjectStatus.FAILED},
        ProjectStatus.FAILED: {ProjectStatus.PARSING},  # 允许断点续跑
        ProjectStatus.COMPLETED: set(),  # 终态
        ProjectStatus.CANCELLED: {ProjectStatus.PARSING},  # 允许重新启动
    }

    def __init__(self):
        # 内存中的状态缓存（进程内单例）
        self._states: dict[int, ProjectState] = {}

    def load_state(self, session: Session, project_id: int) -> ProjectState:
        """从数据库加载状态（如果内存中没有）"""
        if project_id in self._states:
            return self._states[project_id]

        project = session.get(Project, project_id)
        if not project:
            raise ValueError(f"项目 {project_id} 不存在")

        state = ProjectState(
            project_id=project_id,
            status=ProjectStatus(project.status),
            current_stage=StageType(project.current_stage) if project.current_stage else None,
            error=project.error,
            is_running_in_memory=False,
            last_update=project.updated_at,
        )
        self._states[project_id] = state
        return state

    def transition(
        self,
        session: Session,
        project_id: int,
        to_status: ProjectStatus,
        stage: Optional[StageType] = None,
        error: Optional[str] = None,
        log_message: Optional[str] = None,
    ) -> ProjectState:
        """执行状态转换（原子操作）

        Args:
            session: 数据库会话
            project_id: 项目ID
            to_status: 目标状态
            stage: 当前阶段
            error: 错误信息（仅在失败时）
            log_message: 日志消息（可选）

        Returns:
            新的状态对象

        Raises:
            StateTransitionError: 非法状态转换
        """
        state = self.load_state(session, project_id)

        # 验证状态转换合法性
        if to_status not in self.VALID_TRANSITIONS.get(state.status, set()):
            raise StateTransitionError(
                f"非法状态转换: {state.status.value} -> {to_status.value}"
            )

        # 更新状态
        old_status = state.status
        state.status = to_status
        state.current_stage = stage
        state.error = error if to_status == ProjectStatus.FAILED else None
        state.last_update = datetime.now()

        # 持久化到数据库（快照）
        project = session.get(Project, project_id)
        project.status = to_status.value
        project.current_stage = stage.value if stage else None
        project.error = error
        session.commit()

        # 记录状态变更日志
        message = log_message or f"状态变更: {old_status.value} -> {to_status.value}"
        session.add(PipelineLog(
            project_id=project_id,
            level="INFO",
            stage=stage.value if stage else None,
            message=message,
        ))
        session.commit()

        return state

    def mark_running(self, project_id: int) -> bool:
        """标记项目在当前进程中运行（内存标记，不持久化）

        Returns:
            True: 标记成功
            False: 已经在运行中
        """
        if project_id not in self._states:
            raise ValueError(f"项目 {project_id} 状态未加载")

        state = self._states[project_id]
        if state.is_running_in_memory:
            return False

        state.is_running_in_memory = True
        return True

    def unmark_running(self, project_id: int):
        """取消运行标记"""
        if project_id in self._states:
            self._states[project_id].is_running_in_memory = False

    def is_running(self, project_id: int) -> bool:
        """检查是否在当前进程中运行"""
        if project_id not in self._states:
            return False
        return self._states[project_id].is_running_in_memory

    def can_start(self, session: Session, project_id: int) -> tuple[bool, str]:
        """检查项目是否可以启动

        Returns:
            (can_start: bool, reason: str)
        """
        state = self.load_state(session, project_id)

        if state.is_running_in_memory:
            return False, "项目正在运行中"

        if state.status not in {ProjectStatus.READY, ProjectStatus.FAILED, ProjectStatus.CANCELLED}:
            return False, f"当前状态 {state.status.value} 不允许启动"

        # 检查是否有文档
        from db.models import Document
        has_docs = session.query(Document).filter_by(project_id=project_id).count() > 0
        if not has_docs:
            return False, "请先上传需求文档"

        return True, ""

    def reset(self, session: Session, project_id: int):
        """重置项目状态为初始状态"""
        if project_id in self._states:
            del self._states[project_id]

        project = session.get(Project, project_id)
        if project:
            project.status = ProjectStatus.CREATED.value
            project.current_stage = None
            project.error = None
            session.commit()


# 全局单例
_state_manager = StateManager()


def get_state_manager() -> StateManager:
    """获取全局状态管理器"""
    return _state_manager
