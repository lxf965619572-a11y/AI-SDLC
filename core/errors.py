"""增强的错误处理和可观测性系统。

主要改进：
1. 结构化的错误类型（而非万能的 Exception）
2. 错误追踪和上下文记录
3. 降级策略的显式标记
4. 错误指标收集
"""
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class ErrorSeverity(Enum):
    """错误严重级别"""
    DEBUG = "debug"       # 调试信息
    INFO = "info"         # 信息性消息
    WARNING = "warning"   # 警告（可恢复）
    ERROR = "error"       # 错误（可能部分失败）
    CRITICAL = "critical" # 严重错误（系统级故障）


class ErrorCategory(Enum):
    """错误分类"""
    VALIDATION = "validation"       # 输入验证错误
    LLM_CALL = "llm_call"          # LLM调用错误
    PARSING = "parsing"            # 文档解析错误
    DATABASE = "database"          # 数据库错误
    STATE_TRANSITION = "state_transition"  # 状态转换错误
    NETWORK = "network"            # 网络错误
    TIMEOUT = "timeout"            # 超时错误
    RESOURCE = "resource"          # 资源不足（内存、磁盘等）
    BUSINESS_LOGIC = "business_logic"  # 业务逻辑错误
    UNKNOWN = "unknown"            # 未分类错误


@dataclass
class ErrorContext:
    """错误上下文：记录错误发生时的环境信息"""
    project_id: Optional[int] = None
    stage: Optional[str] = None
    artifact_id: Optional[int] = None
    document_id: Optional[int] = None
    batch_id: Optional[int] = None  # LLM批次ID
    retry_count: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "stage": self.stage,
            "artifact_id": self.artifact_id,
            "document_id": self.document_id,
            "batch_id": self.batch_id,
            "retry_count": self.retry_count,
            "extra": self.extra,
        }


@dataclass
class ErrorRecord:
    """错误记录：结构化的错误信息"""
    category: ErrorCategory
    severity: ErrorSeverity
    message: str
    context: ErrorContext
    timestamp: datetime = field(default_factory=datetime.now)
    exception: Optional[Exception] = None
    traceback_str: Optional[str] = None
    fallback_used: bool = False  # 是否使用了降级策略
    fallback_reason: Optional[str] = None

    def __post_init__(self):
        if self.exception and not self.traceback_str:
            self.traceback_str = ''.join(
                traceback.format_exception(type(self.exception), self.exception, self.exception.__traceback__)
            )

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "message": self.message,
            "context": self.context.to_dict(),
            "timestamp": self.timestamp.isoformat(),
            "exception_type": type(self.exception).__name__ if self.exception else None,
            "traceback": self.traceback_str,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
        }

    def format_for_user(self) -> str:
        """格式化为用户友好的消息"""
        parts = [f"[{self.severity.value.upper()}] {self.message}"]

        if self.context.project_id:
            parts.append(f"项目ID: {self.context.project_id}")
        if self.context.stage:
            parts.append(f"阶段: {self.context.stage}")
        if self.fallback_used:
            parts.append(f"已启用降级策略: {self.fallback_reason}")

        return " | ".join(parts)

    def format_for_log(self) -> str:
        """格式化为详细日志"""
        parts = [
            f"[{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}]",
            f"[{self.category.value}]",
            f"[{self.severity.value.upper()}]",
            self.message
        ]

        if self.context.project_id:
            parts.append(f"| Project={self.context.project_id}")
        if self.context.stage:
            parts.append(f"Stage={self.context.stage}")
        if self.context.retry_count > 0:
            parts.append(f"Retry={self.context.retry_count}")
        if self.fallback_used:
            parts.append(f"Fallback={self.fallback_reason}")

        return " ".join(parts)


class ErrorTracker:
    """错误追踪器：收集和分析错误"""

    def __init__(self):
        self._errors: list[ErrorRecord] = []

    def record(self, error: ErrorRecord):
        """记录错误"""
        self._errors.append(error)

        # 输出到日志
        print(error.format_for_log())

        # 严重错误立即输出详细信息
        if error.severity in {ErrorSeverity.ERROR, ErrorSeverity.CRITICAL}:
            if error.traceback_str:
                print(error.traceback_str)

    def get_errors(
        self,
        project_id: Optional[int] = None,
        category: Optional[ErrorCategory] = None,
        severity: Optional[ErrorSeverity] = None
    ) -> list[ErrorRecord]:
        """查询错误记录"""
        results = self._errors

        if project_id is not None:
            results = [e for e in results if e.context.project_id == project_id]
        if category is not None:
            results = [e for e in results if e.category == category]
        if severity is not None:
            results = [e for e in results if e.severity == severity]

        return results

    def get_statistics(self, project_id: Optional[int] = None) -> dict:
        """获取错误统计"""
        errors = self.get_errors(project_id=project_id)

        stats = {
            "total": len(errors),
            "by_category": {},
            "by_severity": {},
            "fallback_count": sum(1 for e in errors if e.fallback_used),
        }

        for error in errors:
            # 按分类统计
            cat = error.category.value
            stats["by_category"][cat] = stats["by_category"].get(cat, 0) + 1

            # 按严重性统计
            sev = error.severity.value
            stats["by_severity"][sev] = stats["by_severity"].get(sev, 0) + 1

        return stats

    def clear(self, project_id: Optional[int] = None):
        """清理错误记录"""
        if project_id is None:
            self._errors.clear()
        else:
            self._errors = [e for e in self._errors if e.context.project_id != project_id]


# 全局错误追踪器
_error_tracker = ErrorTracker()


def get_error_tracker() -> ErrorTracker:
    """获取全局错误追踪器"""
    return _error_tracker


def record_error(
    category: ErrorCategory,
    severity: ErrorSeverity,
    message: str,
    context: ErrorContext,
    exception: Optional[Exception] = None,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None
):
    """便捷函数：记录错误"""
    error = ErrorRecord(
        category=category,
        severity=severity,
        message=message,
        context=context,
        exception=exception,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
    )
    _error_tracker.record(error)


# ==================== 业务特定的错误类 ====================

class WorkbuddyError(Exception):
    """基础业务错误类"""
    def __init__(self, message: str, category: ErrorCategory, context: ErrorContext):
        super().__init__(message)
        self.message = message
        self.category = category
        self.context = context


class LLMCallError(WorkbuddyError):
    """LLM调用错误"""
    def __init__(self, message: str, context: ErrorContext, status_code: Optional[int] = None):
        super().__init__(message, ErrorCategory.LLM_CALL, context)
        self.status_code = status_code


class ValidationError(WorkbuddyError):
    """验证错误"""
    def __init__(self, message: str, context: ErrorContext, field: Optional[str] = None):
        super().__init__(message, ErrorCategory.VALIDATION, context)
        self.field = field


class ParsingError(WorkbuddyError):
    """解析错误"""
    def __init__(self, message: str, context: ErrorContext, document_path: Optional[str] = None):
        super().__init__(message, ErrorCategory.PARSING, context)
        self.document_path = document_path


class StateTransitionError(WorkbuddyError):
    """状态转换错误"""
    def __init__(self, message: str, context: ErrorContext, from_state: str, to_state: str):
        super().__init__(message, ErrorCategory.STATE_TRANSITION, context)
        self.from_state = from_state
        self.to_state = to_state


# ==================== 使用示例 ====================

def example_usage():
    """使用示例（仅供参考）"""

    # 示例1：记录LLM调用失败（带降级）
    context = ErrorContext(project_id=1, stage="requirement", retry_count=2)
    record_error(
        category=ErrorCategory.LLM_CALL,
        severity=ErrorSeverity.WARNING,
        message="LLM API 返回 429 (Too Many Requests)",
        context=context,
        fallback_used=True,
        fallback_reason="使用缓存结果"
    )

    # 示例2：记录验证错误
    try:
        raise ValidationError(
            "测试用例缺少必需字段 'expected'",
            context=ErrorContext(project_id=1, artifact_id=10),
            field="expected"
        )
    except ValidationError as e:
        record_error(
            category=e.category,
            severity=ErrorSeverity.ERROR,
            message=e.message,
            context=e.context,
            exception=e
        )

    # 示例3：查询错误统计
    tracker = get_error_tracker()
    stats = tracker.get_statistics(project_id=1)
    print(f"项目1的错误统计: {stats}")

    # 示例4：查询所有LLM调用错误
    llm_errors = tracker.get_errors(category=ErrorCategory.LLM_CALL)
    print(f"LLM调用错误总数: {len(llm_errors)}")
