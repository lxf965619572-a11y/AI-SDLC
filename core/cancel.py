"""流水线取消标志（跨模块共享，避免循环导入）。"""

_cancel_flags: set[int] = set()


def request_cancel(project_id: int):
    _cancel_flags.add(project_id)


def is_cancelled(project_id: int) -> bool:
    return project_id in _cancel_flags


def clear(project_id: int):
    _cancel_flags.discard(project_id)
