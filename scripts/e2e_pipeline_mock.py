"""全链路编排验收：mock 模型 + 真实验证机，一条命令跑完 parse→…→report。

为什么还要这一个脚本：`scripts/e2e_verify_vm.py` 验的是「判据层」——静态检查、编译、
用例、覆盖率这些确定性结论对不对；但系统能不能真按 V 模型串起来、6 道评审门是否照常被
interrupt 拦住、工具节点的结论能否回灌追溯矩阵并装配成交付件，只有把整张图跑一遍才知道。
真实模型跑一遍要人守 6 道门、要烧 token，不适合当回归；这里用 LLM_MOCK=1 把模型换成
core/mock_c.py 的固定产物，判据层仍然打真实验证机，于是可重复、零 token、无需 Web UI。

数据隔离：临时 SQLite（data/e2e_pipeline/），绝不碰 data/app.sqlite 里的真实项目。

用法：`.venv\\Scripts\\python.exe scripts\\e2e_pipeline_mock.py`
前置：scripts\\setup_verify_vm.ps1 已配好免密；仓库根有 comm_prd.docx（脚本自动生成时可
先跑 scripts\\make_comm_prd.py）。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 必须在导入任何会读它的模块之前置位：离线演示产物由 core.mock_llm 按 role 返回
os.environ["LLM_MOCK"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

# ---- 数据隔离：换库要在 db.models 建 engine 之前 ----
ROOT = config.DATA_DIR / "e2e_pipeline"
ROOT.mkdir(parents=True, exist_ok=True)
(ROOT / "uploads").mkdir(exist_ok=True)
(ROOT / "verify").mkdir(exist_ok=True)
for _old in ("app.sqlite", "checkpoints.sqlite"):
    _p = ROOT / _old
    if _p.exists():
        _p.unlink()
config.APP_DB = ROOT / "app.sqlite"
config.CHECKPOINT_DB = ROOT / "checkpoints.sqlite"
config.UPLOAD_DIR = ROOT / "uploads"
config.VERIFY_EVIDENCE_DIR = ROOT / "verify"

import db.models as M                                              # noqa: E402
from core import trace as T                                        # noqa: E402
from exporters.report_exporter import (export_report_docx,          # noqa: E402
                                       export_report_excel)
from exporters.trace_exporter import export_trace_excel             # noqa: E402
from pipeline import nodes                                          # noqa: E402
from services.report_service import build_report_data               # noqa: E402
from services.trace_service import build_project_matrix             # noqa: E402
from verification import executor                                   # noqa: E402

_FAILS: list[str] = []


def _check(cond: bool, label: str, detail: str = "") -> bool:
    if cond:
        print("  [OK]   " + label)
    else:
        print("  [FAIL] " + label + (("  → " + detail) if detail else ""))
        _FAILS.append(label)
    return bool(cond)


def _step(n: str, title: str) -> None:
    print("\n=== %s %s ===" % (n, title))


def make_project() -> int:
    """建一个干净项目并把样例 PRD 挂上去（parse 阶段要读 Document 行）。"""
    prd = config.BASE_DIR / "comm_prd.docx"
    if not prd.exists():
        raise SystemExit("缺少样例 PRD：%s（先跑 scripts\\make_comm_prd.py）" % prd)
    M.init_db()
    with M.SessionLocal() as s:
        proj = M.Project(name="E2E 全链路验收（mock 模型 + 真实验证机）", status="created")
        s.add(proj)
        s.commit()
        pid = proj.id
        dst = config.UPLOAD_DIR / prd.name
        shutil.copyfile(prd, dst)
        s.add(M.Document(project_id=pid, filename=prd.name,
                         stored_path=str(dst), file_type="docx"))
        s.commit()
    return pid


def run_graph(pid: int) -> tuple[list[str], bool]:
    """同步驱动整张图：遇 interrupt（评审门）就自动通过，直到 END。

    返回 (被拦下的评审门阶段序列, 是否正常收尾)。停在非评审门节点或回环失控都算失败——
    前者意味着某个节点抛错后图卡住，后者意味着失败闭环的轮数上限没起作用。"""
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    from pipeline.graph import build_graph

    gates: list[str] = []
    cfg = {"configurable": {"thread_id": "e2e_%d" % pid}}
    with SqliteSaver.from_conn_string(str(config.CHECKPOINT_DB)) as cp:
        graph = build_graph(cp)
        graph.invoke({"project_id": pid}, config=cfg)
        for _ in range(40):
            snap = graph.get_state(cfg)
            if not snap.next:
                break
            ints = [i for t in snap.tasks for i in (getattr(t, "interrupts", None) or [])]
            if not ints:
                _check(False, "图停在评审门以外的节点", str(snap.next))
                return gates, False
            payload = ints[0].value or {}
            gates.append(payload.get("stage") or "?")
            print("        评审门 %-10s v%s → 自动通过"
                  % (payload.get("stage"), payload.get("version")))
            graph.invoke(Command(resume={"approved": True,
                                         "comments": "E2E 自动通过"}), config=cfg)
        else:
            _check(False, "评审门超过 40 次，失败闭环疑似失控")
            return gates, False
        snap = graph.get_state(cfg)
        return gates, not snap.next


def latest_meta(pid: int, stage: str) -> dict:
    with M.SessionLocal() as s:
        a = (s.query(M.StageArtifact).filter_by(project_id=pid, stage=stage)
             .order_by(M.StageArtifact.version.desc()).first())
        return (a.meta_json or {}) if a else {}


def main() -> int:
    print("航天嵌入式 AI 自动化代码验证系统 —— 全链路编排验收（mock 模型 + 真实验证机）")
    print("验证机：%s@%s:%s　临时库：%s"
          % (config.VERIFY_USER, config.VERIFY_HOST, config.VERIFY_PORT, ROOT))
    if not config.verify_configured():
        print("\n[中止] 未配置验证机（VERIFY_HOST 为空）。先跑 scripts\\setup_verify_vm.ps1。")
        return 2

    pid = make_project()
    print("项目号：%d（临时库，不影响真实数据）" % pid)

    _step("1/4", "驱动整张图：6 道评审门 + 3 个工具节点")
    try:
        gates, finished = run_graph(pid)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("\n[异常] " + type(e).__name__ + ": " + str(e))
        return 1
    from services import pipeline_service as PS

    _check(finished, "图跑到 END（无残留待执行节点）")
    _check(gates == list(nodes.STAGES),
           "评审门顺序 = %s" % ",".join(nodes.STAGES), str(gates))
    with M.SessionLocal() as s:
        proj = s.get(M.Project, pid)
        status = proj.status if proj else "?"
        stage = proj.current_stage if proj else "?"
        reviews = s.query(M.Review).filter_by(project_id=pid).count()
        arts = {a.stage: a for a in
                s.query(M.StageArtifact).filter_by(project_id=pid).all()}
    # 置 completed 是服务层 _run_graph 收尾时做的（本脚本同步驱动图，不经过那一行），
    # 所以这里查图自己负责的事实：走到最后一个阶段、既没停在评审门也没有可续跑节点。
    _check(stage == "report" and status in ("running", "completed"),
           "流水线推进到 report 阶段（状态 %s）" % status, "%s/%s" % (status, stage))
    _check(PS.detect_interrupted_stage(pid) is None, "没有滞留在评审门")
    _check(PS.resumable_stage(pid) is None, "没有待续跑节点（确实终止，不是卡住）")
    _check(reviews >= len(nodes.STAGES), "评审记录留痕 %d 条" % reviews, str(reviews))
    missing = [st for st in nodes.DISPLAY_STAGES if st not in arts]
    _check(not missing, "十个阶段产物齐备：%s" % ",".join(nodes.DISPLAY_STAGES),
           "缺 " + ",".join(missing))
    bad_status = [st for st, a in arts.items() if a.status != "approved"]
    _check(not bad_status, "产物状态均为 approved", str(bad_status))

    _step("2/4", "工具节点结论：静态检查 + 真实机执行")
    sm = latest_meta(pid, "static")
    _check(sm.get("ok") is True and sm.get("required") == 0,
           "静态检查零必查项违规（总违规 %s）" % sm.get("total"), str(sm.get("required")))
    em = latest_meta(pid, "exec")
    v, t, cov = em.get("verdict") or {}, em.get("tests") or {}, em.get("coverage") or {}
    _check(em.get("ok") is True and em.get("decision") == executor.DECISION_NEXT,
           "执行验证全绿（decision=%s）" % em.get("decision"), em.get("reason", ""))
    _check(v.get("build") == "ok" and v.get("tests") == "ok" and v.get("coverage") == "ok",
           "三项判定均 ok", str(v))
    _check(t.get("all_pass") is True, "用例 %s/%s 全过" % (t.get("passed"), t.get("total")),
           str(t.get("failed")))
    _check(cov.get("ok") is True,
           "覆盖率达标 分支 %.2f%%（门限 %.0f%%）"
           % ((cov.get("totals") or {}).get("branch_pct") or 0.0, config.COVERAGE_BRANCH_MIN),
           str(cov.get("totals")))
    env = em.get("env") or {}
    _check(bool(env.get("gcc")) and bool(env.get("gcov")),
           "证据里记了验证机工具链版本：%s" % (env.get("gcc") or "")[:40])
    manifest = em.get("manifest") or {}
    _check(len(manifest) >= 3, "同步输入指纹 %d 项（同步前在本机算出 sha256）"
           % len(manifest), str(sorted(manifest)))
    arch = em.get("evidence")
    _check(bool(arch) and Path(str(arch)).exists(),
           "原始输出已归档：%s" % (arch or "-"), str(arch))

    _step("3/4", "追溯矩阵：四个新列由真实产物填满")
    mx = build_project_matrix(pid)
    rows = mx.get("rows") or []
    summary = mx.get("summary") or {}
    _check(len(rows) > 0, "矩阵 %d 行需求" % len(rows))
    empt = {c: [r["id"] for r in rows if r.get(c) is None]
            for c in ("code_units", "static_violations", "exec_result", "branch_coverage")}
    for c, ids in empt.items():
        _check(not ids, "列 %-18s 全部有值" % c, "空值行 " + str(ids))
    _check(all(r.get("exec_result") == "all" for r in rows),
           "每行执行结果 = 全部通过",
           str({r["id"]: r.get("exec_result") for r in rows}))
    _check(all(T.row_status(r, summary) == T.ST_OK for r in rows),
           "每行状态 = 贯通（未被执行/覆盖维度降级）",
           str({r["id"]: T.row_status(r, summary) for r in rows}))
    xp = str(ROOT / "trace_matrix.xlsx")
    export_trace_excel(mx, xp, "E2E 全链路验收")
    _check(Path(xp).exists() and Path(xp).stat().st_size > 0, "追溯矩阵 xlsx 已导出")

    _step("4/4", "交付件：从库里真实产物装配并导出")
    data = build_report_data(pid)
    concl = data.get("conclusion") or {}
    _check(concl.get("pass") is True, "测评结论：通过", str(concl.get("text")))
    _check(bool((data.get("evidence") or [])), "证据清单非空 %d 项"
           % len(data.get("evidence") or []))
    _check(not (data.get("problems") or []), "无遗留问题报告单 %d 项"
           % len(data.get("problems") or []))
    dx, xx = str(ROOT / "e2e_report.docx"), str(ROOT / "e2e_report.xlsx")
    export_report_docx(data, dx)
    export_report_excel(data, xx)
    try:
        from openpyxl import load_workbook
        sheets = load_workbook(xx).sheetnames
        _check(len(sheets) >= 6 and "证据清单" in sheets, "测评报告 xlsx 可打开（%d 表）"
               % len(sheets), str(sheets))
    except Exception as e:
        _check(False, "测评报告 xlsx 可打开", repr(e))
    try:
        import docx
        d = docx.Document(dx)
        _check(len(d.paragraphs) > 0, "测评报告 docx 可打开（%d 段 / %d 表）"
               % (len(d.paragraphs), len(d.tables)))
    except Exception as e:
        _check(False, "测评报告 docx 可打开", repr(e))
    # report 节点自己也落了一份产物，两者结论必须一致（装配逻辑只有一处）
    rm = latest_meta(pid, "report")
    _check((rm.get("conclusion") or {}).get("pass") is True,
           "report 节点落库产物结论同为通过")
    print("        输出目录：" + str(ROOT))

    print("\n" + "=" * 56)
    if _FAILS:
        print("验收未通过：%d 项失败" % len(_FAILS))
        for f in _FAILS:
            print("  - " + f)
        return 1
    print("验收通过：全链路编排在真实验证机上跑通（评审门 + 工具节点 + 追溯 + 交付件）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
