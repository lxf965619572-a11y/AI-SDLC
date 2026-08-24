"""AI 助手对话接口：基于当前项目产物上下文，与大模型流式对话。"""
from flask import Blueprint, Response, jsonify, request, stream_with_context

from db.models import Project, SessionLocal, StageArtifact
from pipeline.nodes import STAGES, STAGE_TITLES
from core import llm_client
from core.llm_client import LLMError

bp = Blueprint("chat", __name__, url_prefix="/api")

# 单阶段上下文截断长度（防止超上下文窗口）
CTX_MAX_PER_STAGE = 6000

SYSTEM_PROMPT = (
    "你是「多智能体软件开发流水线」系统的 AI 助手。用户正在评审/查看一个软件研发项目，"
    "下方会提供该项目各阶段已产出的文档（结构化原始数据、需求规格说明书、概要设计、"
    "详细设计、测试用例等）作为上下文。\n"
    "你的职责：\n"
    "1. 基于这些产物回答用户问题，引用具体章节/编号时要准确；\n"
    "2. 帮助用户提取、汇总、对比文档中的信息（如需求清单、接口列表、风险项）；\n"
    "3. 如果产物中没有相关信息，明确说明，不要编造；\n"
    "4. 回答用中文，条理清晰，必要时使用 Markdown 表格/列表；\n"
    "5. 用户也可能询问软件工程的通用问题，正常回答即可。"
)


def _build_project_context(pid: int) -> tuple[str, str]:
    """组装项目上下文，返回 (项目名, 上下文文本)。"""
    with SessionLocal() as session:
        p = session.get(Project, pid)
        name = p.name if p else f"项目#{pid}"
        parts = []
        for stage in ["parse", *STAGES]:
            art = (session.query(StageArtifact)
                   .filter_by(project_id=pid, stage=stage)
                   .order_by(StageArtifact.version.desc()).first())
            if art and art.markdown:
                title = STAGE_TITLES.get(stage, stage)
                md = art.markdown[:CTX_MAX_PER_STAGE]
                truncated = "（内容过长已截断）" if len(art.markdown) > CTX_MAX_PER_STAGE else ""
                parts.append(
                    f"\n===== 阶段产物：{title}（v{art.version}） =====\n{md}{truncated}\n")
        return name, "".join(parts) if parts else "（该项目尚无阶段产物）"


@bp.post("/chat")
def chat():
    data = request.json or {}
    pid = data.get("project_id")
    message = (data.get("message") or "").strip()
    history = data.get("history") or []
    if not message:
        return jsonify({"error": "消息不能为空"}), 400
    if not isinstance(history, list):
        history = []

    project_name, ctx = _build_project_context(pid) if pid else ("", "")
    sys_content = SYSTEM_PROMPT
    if ctx:
        sys_content += f"\n\n【当前项目：{project_name}】\n{ctx}"

    # 历史窗口：最多带最近 12 条（6 轮），控制上下文体积
    hist = [m for m in history[-12:]
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)]
    messages = [{"role": "system", "content": sys_content},
                *hist,
                {"role": "user", "content": message}]

    def generate():
        import json as _json
        try:
            for piece in llm_client.chat_stream(messages, role="chat"):
                yield f"data: {_json.dumps({'delta': piece}, ensure_ascii=False)}\n\n"
            yield f"data: {_json.dumps({'done': True})}\n\n"
        except LLMError as e:
            yield f"data: {_json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:  # 兜底：任何未预期错误也走 SSE 告知前端
            yield f"data: {_json.dumps({'error': f'服务内部错误: {e}'}, ensure_ascii=False)}\n\n"

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})
