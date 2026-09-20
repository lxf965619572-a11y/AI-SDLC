"""软件测评报告服务：从库里已落库的产物装配报告数据。

装配逻辑只有这一处：流水线 report 节点与「导出报告」接口共用，
否则前端看到的报告和导出的 docx 会各算一套结论，那是交付件里最不能出的错。
"""
import config
from db.models import Project, SessionLocal, StageArtifact
from exporters.report_exporter import assemble_report
from pipeline.nodes import DISPLAY_STAGES

# 这两个工具阶段可能跑多轮，问题报告单要把失败过的那几轮都留痕
HISTORY_STAGES = ("static", "exec")


def _latest(session, pid: int, stage: str):
    return (session.query(StageArtifact)
            .filter_by(project_id=pid, stage=stage)
            .order_by(StageArtifact.version.desc()).first())


def collect(pid: int) -> tuple[str, dict, dict, dict]:
    """返回 (项目名, arts, versions, history)。"""
    with SessionLocal() as session:
        arts, versions = {}, {}
        for st in DISPLAY_STAGES:
            a = _latest(session, pid, st)
            if not a:
                continue
            arts[st] = {"markdown": a.markdown, "meta": a.meta_json or {},
                        "version": a.version, "status": a.status}
            versions[st] = a.version
        history = {}
        for st in HISTORY_STAGES:
            runs = (session.query(StageArtifact)
                    .filter_by(project_id=pid, stage=st)
                    .order_by(StageArtifact.version.asc()).all())
            history[st] = [{"version": r.version, "status": r.status,
                            "meta": r.meta_json or {}} for r in runs]
        proj = session.get(Project, pid)
        name = (proj.name if proj else "") or f"项目 {pid}"
    return name, arts, versions, history


def build_report_data(pid: int) -> dict:
    """装配报告数据（不落库）。arts 为空时结论会明确写「未产出」。"""
    name, arts, versions, history = collect(pid)
    data = assemble_report(arts, name, versions, history=history,
                           coverage_min=config.COVERAGE_BRANCH_MIN)
    data["thresholds"]["max_fix_rounds"] = config.MAX_FIX_ROUNDS
    return data
