"""项目删除测试：不连验证机、不调模型，只验「删干净了」与「没删错东西」。

删除是整条流水线里唯一不可逆的操作，所以要单测的正是它的两条边界：
  1. 该删的全删：库内记录（文档 / 分块 / 产物 / 评审 / 日志 / 项目本身）、
     LangGraph 检查点、上传原件、导出件目录、验证证据目录，一个不留；
  2. 不该删的一个不动：别的项目、别的目录，以及**预期根之外**的任何路径——
     路径守卫的职责是「配置被改错时宁可漏删」，绝不能递归删到别处。

数据全用临时库与临时目录现场造（换库必须在 import db.models 之前）。
真实验证机链路由 scripts/e2e_pipeline_mock.py 负责，这里一个远端调用都不发。

用法：.venv\\Scripts\\python.exe tests\\test_project_delete.py
"""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                     # noqa: E402

# ---- 数据隔离：换库要在 db.models 建 engine 之前 ----
TMP = Path(tempfile.mkdtemp(prefix="wb_del_test_"))
for _sub in ("outputs", "verify", "uploads", "outside"):
    (TMP / _sub).mkdir(parents=True, exist_ok=True)
config.DATA_DIR = TMP
config.APP_DB = TMP / "app.sqlite"
config.CHECKPOINT_DB = TMP / "checkpoints.sqlite"
config.OUTPUT_DIR = TMP / "outputs"
config.UPLOAD_DIR = TMP / "uploads"
config.VERIFY_EVIDENCE_DIR = TMP / "verify"
config.VERIFY_HOST = ""
config.VERIFY_USER = ""
config.VERIFY_WORKDIR = "wb_verify"

import db.models as M                                              # noqa: E402
from services import pipeline_service, storage                     # noqa: E402

M.init_db()


# ---------------- 造数据 ----------------
def make_project(name="样例项目", status="completed"):
    with M.SessionLocal() as s:
        p = M.Project(name=name, status=status)
        s.add(p)
        s.commit()
        return p.id


def seed_full(pid):
    """给项目塞满各类记录与磁盘产物，返回造出来的路径清单。"""
    up = config.UPLOAD_DIR / f"{pid}_prd.docx"
    up.write_bytes(b"fake docx")
    out = config.project_outputs_dir(pid)
    (out / "sub").mkdir(parents=True, exist_ok=True)
    (out / "report.docx").write_bytes(b"fake docx")
    (out / "sub" / "trace.xlsx").write_bytes(b"fake xlsx")
    ev = config.project_evidence_dir(pid) / "v1t1"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "exec.log").write_text("WB_SECTION run\nTC-001 PASS\n", encoding="utf-8")

    with M.SessionLocal() as s:
        doc = M.Document(project_id=pid, filename="prd.docx",
                         stored_path=str(up), file_type="docx", status="parsed")
        s.add(doc)
        s.commit()
        s.add(M.Chunk(document_id=doc.id, seq=1, content="第一段"))
        art = M.StageArtifact(project_id=pid, stage="requirement", version=1,
                              title="软件需求规格说明书", markdown="# 需求",
                              status="approved")
        s.add(art)
        s.commit()
        s.add(M.Review(project_id=pid, artifact_id=art.id, approved=True,
                       comments="同意"))
        s.add(M.PipelineLog(project_id=pid, message="跑完了"))
        s.commit()
    _seed_checkpoints([(f"proj_{pid}", "c1"), (f"proj_{pid}", "c2")])
    return {"upload": up, "outputs": out, "evidence": config.project_evidence_dir(pid)}


def _seed_checkpoints(rows):
    conn = sqlite3.connect(str(config.CHECKPOINT_DB))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS checkpoints "
                     "(thread_id TEXT, cid TEXT)")
        conn.executemany("INSERT INTO checkpoints (thread_id, cid) VALUES (?, ?)",
                         rows)
        conn.commit()
    finally:
        conn.close()


def _threads():
    conn = sqlite3.connect(str(config.CHECKPOINT_DB))
    try:
        return [r[0] for r in conn.execute("SELECT thread_id FROM checkpoints")]
    finally:
        conn.close()


def _counts(pid):
    with M.SessionLocal() as s:
        return {
            "project": s.query(M.Project).filter_by(id=pid).count(),
            "documents": s.query(M.Document).filter_by(project_id=pid).count(),
            "chunks": s.query(M.Chunk).filter(
                M.Chunk.document_id.in_(
                    s.query(M.Document.id).filter_by(project_id=pid))).count(),
            "artifacts": s.query(M.StageArtifact).filter_by(project_id=pid).count(),
            "reviews": s.query(M.Review).filter_by(project_id=pid).count(),
            "logs": s.query(M.PipelineLog).filter_by(project_id=pid).count(),
        }


# ---------------- 路径守卫 ----------------
def test_remove_file_inside_root():
    root = TMP / "uploads"
    f = root / "guard_ok.txt"
    f.write_text("x", encoding="utf-8")
    assert storage.remove_file(f, root) is True
    assert not f.exists()


def test_remove_file_outside_root_refused():
    """根之外的文件一律不删：即使调用方把路径传错了。"""
    victim = TMP / "outside" / "keep.txt"
    victim.write_text("别删我", encoding="utf-8")
    assert storage.remove_file(victim, config.UPLOAD_DIR) is False
    assert victim.exists()


def test_remove_file_nested_refused():
    """只删根的直接子项：更深的路径说明调用方传错了，不动。"""
    deep = config.UPLOAD_DIR / "a" / "b.txt"
    deep.parent.mkdir(parents=True, exist_ok=True)
    deep.write_text("x", encoding="utf-8")
    assert storage.remove_file(deep, config.UPLOAD_DIR) is False
    assert deep.exists()
    shutil.rmtree(deep.parent, ignore_errors=True)


def test_remove_missing_is_false():
    assert storage.remove_file(config.UPLOAD_DIR / "nope.txt",
                               config.UPLOAD_DIR) is False
    assert storage.remove_tree(config.OUTPUT_DIR / "project_999",
                               config.OUTPUT_DIR) is False


def test_remove_tree_outside_root_refused():
    victim = TMP / "outside" / "dir"
    (victim / "inner").mkdir(parents=True, exist_ok=True)
    (victim / "inner" / "f.txt").write_text("x", encoding="utf-8")
    assert storage.remove_tree(victim, config.OUTPUT_DIR) is False
    assert victim.exists() and (victim / "inner" / "f.txt").exists()


# ---------------- 项目磁盘产物 ----------------
def test_remove_project_dirs_only_touches_that_project():
    pid = make_project("磁盘产物项目")
    mine = seed_full(pid)
    other = make_project("别人的项目")
    theirs = seed_full(other)

    res = storage.remove_project_dirs(pid)
    assert res == {"outputs": True, "evidence": True}, res
    assert not mine["outputs"].exists() and not mine["evidence"].exists()
    assert mine["upload"].exists()          # 上传件归 remove_upload 管，不在这里
    assert theirs["outputs"].exists() and theirs["evidence"].exists()

    assert storage.remove_project_dirs(pid) == {"outputs": False, "evidence": False}


def test_remote_hint_follows_config():
    pid = make_project("远端提示项目")
    assert storage.remote_workdir_hint(pid) == ""       # 未配验证机就不给提示
    config.VERIFY_HOST, config.VERIFY_USER = "10.0.0.9", "lixf"
    try:
        assert storage.remote_workdir_hint(pid) == f"lixf@10.0.0.9:~/wb_verify/p{pid}"
        config.VERIFY_WORKDIR = ""
        assert storage.remote_workdir_hint(pid).endswith(f":~/wb_verify/p{pid}")
    finally:
        config.VERIFY_HOST, config.VERIFY_USER = "", ""
        config.VERIFY_WORKDIR = "wb_verify"


# ---------------- 整项目删除 ----------------
def test_delete_project_removes_everything():
    pid = make_project("待删项目")
    paths = seed_full(pid)
    assert sum(_counts(pid).values()) == 6, _counts(pid)

    removed = pipeline_service.delete_project(pid)

    assert sum(_counts(pid).values()) == 0, _counts(pid)
    assert f"proj_{pid}" not in _threads()
    assert not paths["upload"].exists()
    assert not paths["outputs"].exists()
    assert not paths["evidence"].exists()
    assert removed["documents"] == 1 and removed["artifacts"] == 1, removed
    assert removed["uploads_removed"] == 1 and removed["uploads_total"] == 1
    assert removed["outputs_removed"] and removed["evidence_removed"]
    assert removed["name"] == "待删项目"


def test_delete_project_leaves_others_alone():
    a, b = make_project("甲"), make_project("乙")
    pa, pb = seed_full(a), seed_full(b)
    pipeline_service.delete_project(a)
    assert sum(_counts(b).values()) == 6, _counts(b)
    assert f"proj_{b}" in _threads()
    assert pb["upload"].exists() and pb["outputs"].exists()
    assert pb["evidence"].exists()
    assert not pa["upload"].exists()


def test_delete_missing_project_raises():
    try:
        pipeline_service.delete_project(123456)
    except LookupError:
        return
    raise AssertionError("不存在的项目应抛 LookupError（路由据此回 404）")


def test_delete_running_project_refused():
    """运行中不许删：后台线程还在写库写文件，边删边写必留脏数据。"""
    pid = make_project("运行中项目", status="running")
    seed_full(pid)
    with pipeline_service._lock:
        pipeline_service._running.add(pid)
    try:
        pipeline_service.delete_project(pid)
        raise AssertionError("运行中的项目应拒绝删除")
    except RuntimeError as e:
        assert "运行中" in str(e), e
    finally:
        with pipeline_service._lock:
            pipeline_service._running.discard(pid)
    assert sum(_counts(pid).values()) == 6, _counts(pid)   # 一条都没删

    # 状态位同样拦：即使不在 _running 集合里，status=running 也拒绝
    with M.SessionLocal() as s:
        s.get(M.Project, pid).status = "parsing"
        s.commit()
    try:
        pipeline_service.delete_project(pid)
        raise AssertionError("解析中的项目应拒绝删除")
    except RuntimeError:
        pass


def test_delete_project_without_artifacts():
    """刚建、什么都没跑的项目也要能删（侧栏最常见的清理场景）。"""
    pid = make_project("空项目", status="created")
    removed = pipeline_service.delete_project(pid)
    assert removed["artifacts"] == 0 and removed["uploads_total"] == 0
    assert removed["outputs_removed"] is False        # 没有目录可删，如实报 False
    assert _counts(pid)["project"] == 0


def _cleanup():
    shutil.rmtree(TMP, ignore_errors=True)


def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except Exception as e:
            failed.append(name)
            print("FAIL  " + name + ": " + type(e).__name__ + ": " + str(e))
    print("\n%d/%d passed" % (len(tests) - len(failed), len(tests)))
    _cleanup()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
