"""软件工程包服务：从库里已落库的产物装配整包（文档 + 源码基线 + 追溯 + 证据）。

分工与其它 service 一致：这里只管取数，装配与写盘在 exporters.bundle_exporter。
两边都不调大模型——工程包里的每个字都必须能回溯到某一份已落库产物或某一条
工具输出，现编的结论不能进交付件。
"""
from pathlib import Path

import config
from db.models import Project, SessionLocal, StageArtifact
from exporters.bundle_exporter import (BundleError, plan_bundle, write_bundle,
                                       zip_bundle)
from pipeline.nodes import DISPLAY_STAGES, STAGE_TITLES
from services.trace_service import build_project_matrix


def collect(pid: int) -> tuple:
    """返回 (项目名, {阶段: 全部版本产物})。

    为什么取全版本而不是最新版：源码基线是历史上真正被执行过的那一版，
    验证证据要把失败过的轮次一并收进来。只看最新版，处置过程就被抹掉了。"""
    with SessionLocal() as session:
        proj = session.get(Project, pid)
        if not proj:
            raise BundleError("项目不存在")
        name = proj.name or f"项目 {pid}"
        rows = (session.query(StageArtifact).filter_by(project_id=pid)
                .order_by(StageArtifact.stage, StageArtifact.version.asc()).all())
        arts = {}
        for a in rows:
            arts.setdefault(a.stage, []).append(
                {"version": int(a.version), "status": a.status or "",
                 "title": a.title or STAGE_TITLES.get(a.stage, a.stage),
                 "markdown": a.markdown or "", "meta": a.meta_json or {}})
    return name, arts


def thresholds() -> dict:
    return {"branch_min": config.COVERAGE_BRANCH_MIN,
            "line_min": config.COVERAGE_LINE_MIN,
            "max_fix_rounds": config.MAX_FIX_ROUNDS,
            "complexity_max": config.COMPLEXITY_MAX}


def make_plan(pid: int) -> dict:
    name, arts = collect(pid)
    return plan_bundle(pid, name, arts, stage_order=DISPLAY_STAGES,
                       stage_titles=STAGE_TITLES,
                       evidence_root=config.VERIFY_EVIDENCE_DIR,
                       thresholds=thresholds())


def plan_summary(pid: int) -> dict:
    """只算不写盘：前端在导出前就能看清基线是哪一版、对不对得上、有哪些告警。"""
    plan = make_plan(pid)
    base = plan["baseline"]
    produced = [d for d in plan["docs"] if d["produced"]]
    return {
        "bundle_name": plan["bundle_name"],
        "project_id": plan["project_id"],
        "project_name": plan["project_name"],
        "generated_at": plan["generated_at"],
        "baseline": {"tag": base.get("tag"), "code_version": base.get("code_version"),
                     "test_version": base.get("test_version"),
                     "exec_version": base.get("exec_version"),
                     "exec_ok": base.get("exec_ok"),
                     "exec_status": base.get("exec_status"),
                     "reason": base.get("reason")},
        "aligned": plan["alignment"]["aligned"],
        "mismatched": plan["alignment"]["mismatched"],
        "doc_count": len(produced),
        "missing_stages": plan["missing_stages"],
        "source_files": len(plan["workspace"]),
        "evidence_runs": [{"exec_version": r["exec_version"], "tag": r["tag"],
                           "ok": r["ok"], "status": r["status"],
                           "files": sum(1 for f in r["files"] if f["exists"])}
                          for r in plan["evidence"]],
        "warnings": plan["warnings"],
    }


def build_bundle(pid: int, out_root=None, want_zip: bool = True) -> dict:
    """装配整包并落盘，返回 {dir, zip, files, aligned, warnings, ...}。"""
    plan = make_plan(pid)
    base = plan["baseline"]

    # 追溯矩阵按基线版本钉死：包里的矩阵必须描述包里那份源码，
    # 否则执行后代码又改过时，矩阵会去替一份没被验证过的代码背书。
    matrix = None
    try:
        m = build_project_matrix(pid, pin={"code": base.get("code_version"),
                                           "test_impl": base.get("test_version"),
                                           "exec": base.get("exec_version")})
        matrix = m if m.get("rows") else None
    except Exception:
        matrix = None
    if matrix is None:
        plan["warnings"].append("需求追溯矩阵未产出（缺需求产物），包内 03_追溯 不含矩阵")

    root = Path(out_root) if out_root else (Path(config.OUTPUT_DIR) / f"project_{pid}")
    res = write_bundle(plan, root, matrix=matrix)
    res["matrix_included"] = bool(matrix)
    if want_zip:
        res["zip"] = zip_bundle(res["dir"])
        res["zip_name"] = Path(res["zip"]).name
    return res
