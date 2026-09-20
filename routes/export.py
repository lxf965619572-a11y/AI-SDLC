"""导出 API：测试用例（Excel / XMind）+ 各阶段文档（Word / Markdown）+ 需求追溯矩阵。"""
import re

from flask import Blueprint, jsonify, request, send_file

import config
from db.models import Project, SessionLocal, StageArtifact
from exporters.excel_exporter import export_excel
from exporters.xmind_exporter import export_xmind
from exporters.docx_exporter import export_docx
from exporters.trace_exporter import export_trace_excel
from pipeline.nodes import STAGE_TITLES
from services.trace_service import build_project_matrix

bp = Blueprint("export", __name__, url_prefix="/api")

# 支持文档（Word/Markdown）导出的阶段
DOC_STAGES = ("parse", "requirement", "hld", "lld", "testcase")


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

    out_dir = config.OUTPUT_DIR / f"project_{pid}"
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

    out_dir = config.OUTPUT_DIR / f"project_{pid}"
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

    out_dir = config.OUTPUT_DIR / f"project_{pid}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = export_trace_excel(data, str(out_dir / f"{name}_需求追溯矩阵.xlsx"),
                              project_name=proj_name)
    return send_file(path, as_attachment=True,
                     download_name=f"{name}_需求追溯矩阵.xlsx")
