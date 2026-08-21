"""导出 API：测试用例（Excel / XMind）+ 各阶段文档（Word / Markdown）。"""
import re

from flask import Blueprint, jsonify, request, send_file

import config
from db.models import Project, SessionLocal, StageArtifact
from exporters.excel_exporter import export_excel
from exporters.xmind_exporter import export_xmind
from exporters.docx_exporter import export_docx
from pipeline.nodes import STAGE_TITLES

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
