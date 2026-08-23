"""多智能体软件开发流水线系统 - Flask 入口。"""
import os
import sys

from flask import Flask, render_template

import config
from db.models import Project, SessionLocal, init_db
from routes import export as export_routes
from routes import projects as project_routes
from services import pipeline_service_v2 as pipeline_service

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


@app.get("/")
def index():
    return render_template("index.html")


def restore_waiting_states():
    """进程重启后：自动恢复孤儿项目（新版本已集成所有逻辑）"""
    pipeline_service.auto_resume_orphans()


if __name__ == "__main__":
    # 必须在恢复逻辑之前拿锁：否则第二个实例会先执行 auto_resume_orphans，
    # 对同一项目重复触发流水线
    # TEMP: 临时禁用锁检查（测试完成后记得恢复）
    # if not acquire_single_instance_lock():
    #     print(f"[ERROR] 检测到已有实例正在运行（锁文件被占用：{LOCK_PATH}），"
    #           f"本进程退出以避免重复抽取。", file=sys.stderr)
    #     sys.exit(1)
    print("[WARNING] 单实例检查已临时禁用")
    init_db()
    restore_waiting_states()
    app.run(host=config.APP_HOST, port=config.APP_PORT,
            debug=config.APP_DEBUG, use_reloader=False, threaded=True)
else:
    # 被 WSGI/测试导入时仍保持原有初始化行为
    init_db()
    restore_waiting_states()
