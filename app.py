"""多智能体软件开发流水线系统 - Flask 入口。"""
import os
import sys

from flask import Flask, render_template

import config
from db.models import Project, SessionLocal, init_db
from routes import chat as chat_routes
from routes import export as export_routes
from routes import projects as project_routes
from services import pipeline_service

# 单实例锁文件句柄（保持引用防止 GC 提前释放锁）
_LOCK_FILE = None
LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "server.lock")


def acquire_single_instance_lock() -> bool:
    """防止两个 app.py 实例同时运行——双实例会对同一项目重复触发
    自动续跑，导致 LLM 抽取被两条线程并行执行。"""
    global _LOCK_FILE
    try:
        _LOCK_FILE = open(LOCK_PATH, "w")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(_LOCK_FILE.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_LOCK_FILE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FILE.write(str(os.getpid()))
        _LOCK_FILE.flush()
        return True
    except OSError:
        return False

app = Flask(__name__)
app.register_blueprint(project_routes.bp)
app.register_blueprint(export_routes.bp)
app.register_blueprint(chat_routes.bp)


@app.get("/")
def index():
    return render_template("index.html")


def restore_waiting_states():
    """进程重启后：把『运行中』但实际停在评审门的项目恢复为 waiting_review；
    卡在 running/parsing 的孤儿项目自动断点续跑；无检查点的才置 failed。"""
    with SessionLocal() as session:
        running = session.query(Project).filter(
            Project.status.in_(("running", "parsing", "waiting_review"))).all()
        ids = [p.id for p in running]
    for pid in ids:
        try:
            pipeline_service.try_resume_orphan(pid)
        except Exception:
            pass
    # 自动续跑卡在运行中/解析中的孤儿项目（解析缓存保证不重复抽取）
    pipeline_service.auto_resume_orphans()
    # 仍没有任何检查点记录（从未启动或崩溃在无法恢复的位置）的置为 failed
    with SessionLocal() as session:
        stuck = session.query(Project).filter(
            Project.status.in_(("running", "parsing"))).all()
        for p in stuck:
            if pipeline_service.detect_interrupted_stage(p.id) is None \
                    and pipeline_service.resumable_stage(p.id) is None:
                p.status = "failed"
                p.error = "进程重启时流水线正在执行，请重新启动流水线"
        session.commit()


if __name__ == "__main__":
    # 必须在恢复逻辑之前拿锁：否则第二个实例会先执行 auto_resume_orphans，
    # 对同一项目重复触发流水线
    if not acquire_single_instance_lock():
        print(f"[ERROR] 检测到已有实例正在运行（锁文件被占用：{LOCK_PATH}），"
              f"本进程退出以避免重复抽取。", file=sys.stderr)
        sys.exit(1)
    init_db()
    restore_waiting_states()
    app.run(host=config.APP_HOST, port=config.APP_PORT,
            debug=config.APP_DEBUG, use_reloader=False, threaded=True)
else:
    # 被 WSGI/测试导入时仍保持原有初始化行为
    init_db()
    restore_waiting_states()
