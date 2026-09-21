"""项目在服务器磁盘上的产物位置与删除。

删除不可逆，所以本模块只有一条硬规矩：**任何 unlink / rmtree 之前，先把路径
resolve 出来核对父目录确实是预期的根**。配置被改错（OUTPUT_DIR 指到用户主目录、
证据目录被改成盘符根之类）时宁可漏删一个目录，也绝不递归删到预期之外；
指向别处的软链/junction 会因为 resolve 后父目录对不上而被跳过。

路径一律在**调用时**从 config 读取，不在 import 期固化：单测与 e2e 脚本都会把
config.*_DIR 重定向到临时目录，import 期取值的模块会让它们删到真实 data/ 下。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import config


def _direct_child(path, root):
    """path 解析后确实是 root 的直接子项则返回该 Path，否则返回 None。"""
    try:
        p = Path(str(path)).resolve()
        r = Path(str(root)).resolve()
    except OSError:
        return None
    return p if p.parent == r else None


def remove_file(path, root) -> bool:
    """删 root 下的一个文件；不在 root 下、不存在或删不掉都返回 False。"""
    p = _direct_child(path, root)
    if p is None or not p.is_file():
        return False
    try:
        p.unlink()
    except OSError:
        return False
    return True


def remove_tree(path, root) -> bool:
    """删 root 下的一个目录（递归）；判定同上。"""
    p = _direct_child(path, root)
    if p is None or not p.is_dir():
        return False
    shutil.rmtree(p, ignore_errors=True)
    return not p.exists()


def remove_upload(stored_path) -> bool:
    """删一个上传的需求文档原件。"""
    return remove_file(stored_path, config.UPLOAD_DIR)


def remove_project_dirs(pid: int) -> dict:
    """删掉该项目在服务器磁盘上的导出件与验证证据，返回各自是否删掉。"""
    return {
        "outputs": remove_tree(config.project_outputs_dir(pid), config.OUTPUT_DIR),
        "evidence": remove_tree(config.project_evidence_dir(pid),
                                config.VERIFY_EVIDENCE_DIR),
    }


def remote_workdir_hint(pid: int) -> str:
    """验证机上该项目的工作区位置。

    删除**不触碰远端**：验证机可能不可达，而删除必须在离线时也能成功；
    自动 rm 远端目录还要把 .env 里的路径拼进 shell 命令，配置写错的后果比
    留着几个目录严重得多。所以只把位置告诉用户，由人自己决定要不要清。"""
    host = (config.VERIFY_HOST or "").strip()
    if not host:
        return ""
    user = (config.VERIFY_USER or "").strip()
    root = (config.VERIFY_WORKDIR or "wb_verify").strip() or "wb_verify"
    who = f"{user}@" if user else ""
    return f"{who}{host}:~/{root}/p{pid}"
