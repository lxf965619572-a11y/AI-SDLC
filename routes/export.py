"""导出 API：测试用例（Excel / XMind）+ 各阶段文档（Word / Markdown）+ 需求追溯矩阵
+ 软件工程包（文档 / 源码基线 / 追溯件 / 验证证据 一整包）。"""
import re

from flask import Blueprint, jsonify, request, send_file

import config
from db.models import Project, SessionLocal, StageArtifact
from exporters.bundle_exporter import BundleError
from exporters.excel_exporter import export_excel
from exporters.xmind_exporter import export_xmind
from exporters.docx_exporter import export_docx
from exporters.trace_exporter import export_trace_excel
from exporters.report_exporter import export_report_docx, export_report_excel
from pipeline.nodes import STAGE_TITLES
from services import bundle_service
from services.report_service import build_report_data
from services.trace_service import build_project_matrix

bp = Blueprint("export", __name__, url_prefix="/api")

# 支持文档（Word/Markdown）导出的阶段
DOC_STAGES = ("parse", "requirement", "hld", "lld", "testcase",
              "code", "test_impl", "static", "exec", "report")


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:60] or "project"


@bp.get("/projects/<int:pid>/export/testcases")
def export_testcases(pid: int):
    fmt = request.args.get("format", "excel").lower()
    if fmt not in ("excel", "xmind"):
        return jsonify({"error": "format 参数必须为 excel 或 xmind"}), 400

    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        art = (session.query(StageArtifact)
               .filter_by(project_id=pid, stage="testcase")
               .order_by(StageArtifact.version.desc()).first())
        if not art or not art.meta_json or not art.meta_json.get("testcases"):
            return jsonify({"error": "测试用例尚未生成"}), 404
        cases = art.meta_json["testcases"]
        name = _safe_name(p.name)

    out_dir = config.project_outputs_dir(pid)
    out_dir.mkdir(parents=True, exist_ok=True)

    if fmt == "excel":
        path = export_excel(cases, str(out_dir / f"{name}_测试用例.xlsx"),
                            sheet_title="测试用例")
        return send_file(path, as_attachment=True,
                         download_name=f"{name}_测试用例.xlsx")
    path = export_xmind(cases, str(out_dir / f"{name}_测试用例.xmind"),
                        root_title=f"{name} 测试用例")
    return send_file(path, as_attachment=True,
                     download_name=f"{name}_测试用例.xmind")


@bp.get("/projects/<int:pid>/stages/<stage>/export")
def export_stage_doc(pid: int, stage: str):
    """导出各阶段文档：format=docx（Word）或 md（Markdown 原文）。"""
    fmt = request.args.get("format", "docx").lower()
    if fmt not in ("docx", "md"):
        return jsonify({"error": "format 参数必须为 docx 或 md"}), 400
    if stage not in DOC_STAGES:
        return jsonify({"error": f"阶段 {stage} 不支持导出"}), 400

    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        art = (session.query(StageArtifact)
               .filter_by(project_id=pid, stage=stage)
               .order_by(StageArtifact.version.desc()).first())
        if not art or not art.markdown:
            return jsonify({"error": f"「{STAGE_TITLES.get(stage, stage)}」尚未生成"}), 404
        markdown = art.markdown
        title = art.title
        name = _safe_name(p.name)

    out_dir = config.project_outputs_dir(pid)
    out_dir.mkdir(parents=True, exist_ok=True)

    if fmt == "md":
        path = str(out_dir / f"{name}_{title}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(markdown)
        return send_file(path, as_attachment=True,
                         download_name=f"{name}_{title}.md",
                         mimetype="text/markdown")

    path = export_docx(markdown, str(out_dir / f"{name}_{title}.docx"), title=title)
    return send_file(path, as_attachment=True,
                     download_name=f"{name}_{title}.docx")


@bp.get("/projects/<int:pid>/export/traceability")
def export_traceability(pid: int):
    """导出需求追溯矩阵 Excel：需求 → 概设 → 详设 → 用例 全链路，可作交付件归档。"""
    fmt = request.args.get("format", "excel").lower()
    if fmt != "excel":
        return jsonify({"error": "format 参数目前只支持 excel"}), 400

    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        proj_name, name = p.name, _safe_name(p.name)

    data = build_project_matrix(pid)
    if not data.get("rows"):
        return jsonify({"error": "需求产物尚未生成，暂无可导出的追溯矩阵"}), 404

    out_dir = config.project_outputs_dir(pid)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = export_trace_excel(data, str(out_dir / f"{name}_需求追溯矩阵.xlsx"),
                              project_name=proj_name)
    return send_file(path, as_attachment=True,
                     download_name=f"{name}_需求追溯矩阵.xlsx")


@bp.get("/projects/<int:pid>/export/report")
def export_report(pid: int):
    """导出软件测评报告（docx / xlsx）。

    数据一律走 report_service.build_report_data，和流水线 report 节点同一套装配，
    避免导出的报告与前端看到的结论对不上。"""
    fmt = request.args.get("format", "docx").lower()
    if fmt not in ("docx", "xlsx"):
        return jsonify({"error": "format 参数必须为 docx 或 xlsx"}), 400

    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        name = _safe_name(p.name)

    data = build_report_data(pid)
    arts = data.get("artifacts") or {}
    if not arts.get("exec") and not arts.get("static"):
        return jsonify({"error": "测评报告尚未生成：需先完成静态检查或验证执行"}), 404

    out_dir = config.project_outputs_dir(pid)
    out_dir.mkdir(parents=True, exist_ok=True)
    if fmt == "xlsx":
        fname = f"{name}_软件测评报告附表.xlsx"
        path = export_report_excel(data, str(out_dir / fname))
    else:
        fname = f"{name}_软件测评报告.docx"
    path = export_report_docx(data, str(out_dir / fname))
    return send_file(path, as_attachment=True, download_name=fname)


@bp.get("/projects/<int:pid>/export/bundle")
def export_bundle(pid: int):
    """导出软件工程包。

    format=zip   下载整包（默认）
    format=dir   只在服务器 OUTPUT_DIR 下生成目录，返回路径与核对结论（便于本地打开）
    format=plan  不落盘，只返回装配计划摘要：前端在导出前就能报出基线版本与告警

    包内源码基线取「最后一次验证执行真正跑过的那一版」，并与该轮证据的输入指纹
    逐字节核对；对不上不拦导出，但会在包清单里显式标红——交付件宁可难看，不可含糊。"""
    fmt = request.args.get("format", "zip").lower()
    if fmt not in ("zip", "dir", "plan"):
        return jsonify({"error": "format 参数必须为 zip、dir 或 plan"}), 400

    with SessionLocal() as session:
        p = session.get(Project, pid)
        if not p:
            return jsonify({"error": "项目不存在"}), 404
        name = _safe_name(p.name)

    try:
        if fmt == "plan":
            return jsonify(bundle_service.plan_summary(pid))
        res = bundle_service.build_bundle(pid, want_zip=(fmt == "zip"))
    except BundleError as e:
        return jsonify({"error": str(e)}), 404
    except OSError as e:
        return jsonify({"error": f"工程包生成失败：{e}"}), 500

    if fmt == "dir":
        return jsonify({"dir": res["dir"], "name": res["name"],
                        "file_count": res["file_count"],
                        "files": [f["path"] for f in res["files"]],
                        "aligned": res["aligned"], "mismatched": res["mismatched"],
                        "warnings": res["warnings"],
                        "baseline": {k: res["baseline"].get(k) for k in
                                     ("tag", "code_version", "test_version",
                                      "exec_version", "exec_ok", "reason")}})
    return send_file(res["zip"], as_attachment=True,
                     download_name=f"{name}_软件工程包.zip")
