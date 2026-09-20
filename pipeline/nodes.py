"""流水线节点：解析抽取、智能体节点、评审门控节点、验证工具节点。
每个节点自管 DB session；产物写入 stage_artifacts（驳回重跑时版本+1）。

节点分两类：
  智能体节点（STAGES）  有 LLM 参与，产物必须过人工评审门才能往下走；
  工具节点（TOOL_STAGES）没有 LLM，产物就是工具链结论本身——静态检查报告、
                        验证执行报告、交付报告。结论由判据给出，不需要「评审文风」，
                        但结论不通过时必须有人工裁决入口，不允许静默放行。
"""
import json
import traceback

import config
from core import stream_bus
from core import c_files, c_rules, c_static
from db.models import (Chunk, Document, PipelineLog, Project, Review,
                       SessionLocal, StageArtifact)
from pipeline.state import PipelineState
from verification import executor
from verification.runner import runner_from_config

# 有人工评审门的智能体阶段
STAGES = ["requirement", "hld", "lld", "testcase", "code", "test_impl"]
# 纯工具节点：无 LLM、无文风评审，产物即判据结论
TOOL_STAGES = ["static", "exec", "report"]
# 前端与状态接口按流水线真实顺序展示（static 夹在 code 与 test_impl 之间）
DISPLAY_STAGES = ["parse", "requirement", "hld", "lld", "testcase",
                  "code", "static", "test_impl", "exec", "report"]
STAGE_TITLES = {
    "parse": "结构化原始数据",
    "requirement": "软件需求规格说明书",
    "hld": "概要设计说明书",
    "lld": "详细设计说明书",
    "testcase": "测试用例设计",
    "code": "代码实现",
    "static": "静态检查报告",
    "test_impl": "测试实现",
    "exec": "代码验证执行报告",
    "report": "软件测评报告",
}

# 评审门通过后的去向。代码阶段先过静态检查，测试实现阶段直接进执行验证——
# 这两个工具节点不设评审门，所以它们不在 STAGES 里，只能在路由表里显式接上。
GATE_NEXT = {
    "requirement": "agent_hld",
    "hld": "agent_lld",
    "lld": "agent_testcase",
    "testcase": "agent_code",
    "code": "static_node",
    "test_impl": "exec_node",
}

# 人工裁决与自动修复额度的关系。额度清零 = 重新给一轮完整的自动整改机会：
#   工具裁决门（static/exec）：人工在这里对工具结论本身表态（偏差单放行、问题报告单
#     受理或驳回），通过与驳回都算重新给基线；
#   文档评审门（code/test_impl）：只有驳回算人工介入。通过是放行自动闭环的下一轮，
#     额度必须接着累计——自动整改每轮都要再过一次代码评审门，门一通过就把计数清零的
#     话，MAX_FIX_ROUNDS 永远触发不了，超限的问题报告单与人工裁决入口形同虚设，
#     失败可以靠着「重生→过门→再失败」无限循环下去。
TOOL_GATE_STAGES = ("static", "exec")
RESET_FIX_ROUNDS = ("code", "test_impl", "static", "exec")


def gate_fix_rounds(stage: str, approved: bool) -> dict | None:
    """评审门对自动修复额度的处置：None 表示不动，字典表示清零（规则见常量注释）。"""
    if stage not in RESET_FIX_ROUNDS:
        return None
    if approved and stage not in TOOL_GATE_STAGES:
        return None
    return {"static": 0, "exec": 0}


def log(session, project_id: int, message: str, stage: str | None = None,
        level: str = "INFO"):
    session.add(PipelineLog(project_id=project_id, level=level, stage=stage,
                            message=message))
    session.commit()


def _set_status(session, project_id: int, status: str, stage: str | None = None):
    proj = session.get(Project, project_id)
    if proj:
        proj.status = status
        if stage:
            proj.current_stage = stage
        session.commit()


def _save_artifact(session, project_id: int, stage: str, markdown: str,
                   meta: dict | None, approved_status: bool = False,
                   status: str | None = None) -> int:
    """保存产物；同阶段已有产物则版本+1。返回 artifact_id。

    status 显式给定时优先——工具节点没有文风评审门，结论由判据直接定状态：
    通过 approved、未通过且自动回环 rejected、超限待人工裁决 pending_review。"""
    prev = session.query(StageArtifact).filter_by(
        project_id=project_id, stage=stage).count()
    art = StageArtifact(
        project_id=project_id, stage=stage, version=prev + 1,
        title=STAGE_TITLES.get(stage, stage), markdown=markdown,
        meta_json=meta,
        status=status or ("approved" if approved_status else "pending_review"),
    )
    session.add(art)
    session.commit()
    return art.id


def _get_artifact(session, artifact_id: int) -> StageArtifact | None:
    return session.get(StageArtifact, artifact_id)


# ---------------- 阶段1：输入解析与抽取 ----------------
def parse_input(state: PipelineState) -> dict:
    from parsing.chunker import chunk_paragraphs
    from parsing.doc_parser import parse_document
    from parsing.extractor import extract_structured, render_markdown

    project_id = state["project_id"]
    with SessionLocal() as session:
        _set_status(session, project_id, "parsing", "parse")
        stream_bus.publish(project_id, type="stage_start", stage="parse")
        log(session, project_id, "开始解析上传文档", "parse")
        docs = session.query(Document).filter_by(project_id=project_id).all()
        if not docs:
            raise RuntimeError("项目没有上传任何文档")

        # 重跑保护：清理本项目旧分块，避免重复累积
        for doc in docs:
            session.query(Chunk).filter_by(document_id=doc.id).delete()
        session.commit()

        all_chunks = []
        for doc in docs:
            try:
                paras = parse_document(doc.stored_path, doc.file_type)
                chunks = chunk_paragraphs(paras)
                for c in chunks:
                    c["doc"] = doc.filename   # 供来源标记 D{文档序号}C{段序号} 使用
                    session.add(Chunk(document_id=doc.id, seq=c["seq"],
                                      heading=c["heading"], content=c["content"]))
                doc.status = "parsed"
                session.commit()
                all_chunks.extend(chunks)
                log(session, project_id, f"文档 {doc.filename} 解析完成，{len(chunks)} 个分块", "parse")
            except Exception as e:
                doc.status = "failed"
                doc.parse_error = str(e)
                session.commit()
                log(session, project_id, f"文档 {doc.filename} 解析失败: {e}", "parse", "ERROR")

        if not all_chunks:
            raise RuntimeError("所有文档解析失败，无可抽取内容")

        total = len(all_chunks)
        from parsing.extractor import BATCH_SIZE
        n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        log(session, project_id,
            f"开始 LLM 抽取，共 {total} 个分块，分 {n_batches} 批并发抽取（每批 {BATCH_SIZE} 块）", "parse")
        last_pct = {"v": 0}
        import time as _time
        t0 = _time.time()
        live = {"done": 0}  # 真实 LLM 批次数（不含缓存命中），用于 ETA

        def progress_cb(done: int, tot: int, cached: bool = False):
            if not cached:
                live["done"] += 1
            pct = done * 100 // tot
            # 解析阶段是并发批次抽取，没有可展示的连续文本，只推批次进度
            stream_bus.publish(project_id, type="progress", stage="parse",
                               done=done, total=tot, pct=pct, cached=cached)
            # 每推进 5% 或首批完成记一条进度日志（含预计剩余时间）
            if done == 1 or pct >= last_pct["v"] + 5 or done == tot:
                last_pct["v"] = pct
                if not cached and live["done"] > 0:
                    eta_s = (_time.time() - t0) / live["done"] * (tot - done)
                    eta = f"，预计还需约 {int(eta_s // 60)} 分 {int(eta_s % 60)} 秒"
                else:
                    eta = ""
                hit = "（缓存命中）" if cached else ""
                log(session, project_id,
                    f"LLM 抽取进度：第 {done}/{tot} 批完成（{pct}%）{hit}{eta}", "parse")

        def reduce_cb(message: str):
            stream_bus.publish(project_id, type="progress", stage="parse",
                               message=f"合并去重：{message}")
            log(session, project_id, f"合并去重：{message}", "parse")

        structured = extract_structured(all_chunks, progress_cb=progress_cb,
                                        reduce_cb=reduce_cb, project_id=project_id)
        md = render_markdown(structured, [d.filename for d in docs if d.status == "parsed"])
        art_id = _save_artifact(session, project_id, "parse", md, structured,
                                approved_status=True)  # 解析阶段自动通过，无需评审
        stream_bus.publish(project_id, type="stage_done", stage="parse",
                           version=1, artifact_id=art_id, chars=len(md))
        log(session, project_id, "结构化抽取完成", "parse")
        return {"structured": structured,
                "artifacts": {"parse": {"id": art_id, "version": 1}}}


# ---------------- 阶段2-5：智能体节点工厂 ----------------
def _get_approved(session, project_id: int, stage: str) -> StageArtifact | None:
    """取该阶段最新一版产物。"""
    return (session.query(StageArtifact)
            .filter_by(project_id=project_id, stage=stage)
            .order_by(StageArtifact.version.desc()).first())


def _make_compile_check(project_id: int, stage: str, session=None,
                        code_files: dict | None = None):
    """造一个「把生成物送验证机编译」的硬校验器，供 run_agent 的 doc_validator 用。

    代码阶段只编 src/ 与 include/；测试实现阶段连同被测代码一起编译并链接——
    链接通过才说明测试程序真能跑起来，只编不链会把缺 main、符号未定义这类问题
    留到执行阶段才暴露，那时已经浪费了一整轮同步与覆盖率采集。
    未配置验证机时 compile_probe 返回 skipped=True，离线/mock 模式照常往下走。
    """
    link = stage == "test_impl"

    def check(files: dict) -> dict:
        probe = executor.compile_probe(
            runner_from_config(), project_id,
            code_files if link else files,
            files if link else None,
            stage=stage)
        if session is not None:
            what = "编译并链接" if link else "编译"
            if probe.get("skipped"):
                log(session, project_id, f"未配置验证机，跳过远端{what}校验",
                    stage, "WARN")
            elif probe.get("ok"):
                log(session, project_id,
                    f"远端{what}通过（退出码 {probe.get('exit_code')}）", stage)
            else:
                tail = "\n".join((probe.get("log") or "").strip().splitlines()[-12:])
                log(session, project_id,
                    f"远端{what}未通过，回灌工具输出重新生成：\n{tail}",
                    stage, "WARN")
        return probe

    return check


def make_agent(stage: str):
    def agent_node(state: PipelineState) -> dict:
        from agents import stage_agents

        project_id = state["project_id"]
        retry_comments = state.get("retry_comments")
        with SessionLocal() as session:
            _set_status(session, project_id, "running", stage)
            prev_art = _get_approved(session, project_id, stage)
            prev_md = prev_art.markdown if (retry_comments and prev_art) else None
            log(session, project_id,
                f"{'带评审意见修订' if retry_comments else '生成'}「{STAGE_TITLES[stage]}」", stage)

            # 流式生成进度：按累计字符数每推进 ~2000 字记一条日志
            last_chars = {"v": 0}

            def gen_cb(chars: int):
                if chars - last_chars["v"] >= 2000:
                    last_chars["v"] = chars
                    log(session, project_id,
                        f"「{STAGE_TITLES[stage]}」生成中，已输出约 {chars} 字符", stage)

            try:
                # 绑定流式通道：期间 llm_client 收到的每个增量都会实时推给前端，
                # 不必等整份文档校验通过并落库
                with stream_bus.bind(project_id, stage):
                    if stage == "requirement":
                        md, meta = stage_agents.analyze_requirements(
                            json.dumps(state["structured"], ensure_ascii=False),
                            retry_comments, prev_md, progress_cb=gen_cb)
                    elif stage == "hld":
                        srs = _get_approved(session, project_id, "requirement")
                        md, meta = stage_agents.design_hld(
                            srs.markdown, srs.meta_json or {}, retry_comments, prev_md,
                            progress_cb=gen_cb)
                    elif stage == "lld":
                        hld = _get_approved(session, project_id, "hld")
                        srs = _get_approved(session, project_id, "requirement")
                        md, meta = stage_agents.design_lld(
                            hld.markdown, hld.meta_json or {}, srs.markdown,
                            srs.meta_json or {},
                            retry_comments, prev_md, progress_cb=gen_cb)
                    elif stage == "testcase":
                        srs = _get_approved(session, project_id, "requirement")
                        md, meta = stage_agents.generate_testcases(
                            srs.markdown, srs.meta_json or {}, retry_comments, prev_md,
                            progress_cb=gen_cb)
                    elif stage == "code":
                        lld = _get_approved(session, project_id, "lld")
                        srs = _get_approved(session, project_id, "requirement")
                        if not lld:
                            raise RuntimeError("缺少详细设计产物，无法生成代码")
                        md, meta = stage_agents.generate_code(
                            lld.markdown, lld.meta_json or {},
                            (srs.meta_json if srs else None),
                            retry_comments, prev_md,
                            compile_check=_make_compile_check(project_id, stage,
                                                              session),
                            progress_cb=gen_cb)
                    elif stage == "test_impl":
                        tc = _get_approved(session, project_id, "testcase")
                        code = _get_approved(session, project_id, "code")
                        lld = _get_approved(session, project_id, "lld")
                        srs = _get_approved(session, project_id, "requirement")
                        if not tc:
                            raise RuntimeError("缺少测试用例设计产物，无法生成测试实现")
                        code_files = (c_files.extract_files(code.markdown)
                                      if code else {})
                        md, meta = stage_agents.generate_test_impl(
                            tc.meta_json or {}, code_files,
                            (lld.meta_json if lld else None),
                            (srs.meta_json if srs else None),
                            retry_comments, prev_md,
                            compile_check=_make_compile_check(
                                project_id, stage, session,
                                code_files=code_files),
                            progress_cb=gen_cb)
                    else:
                        raise ValueError(f"未知阶段: {stage}")
            except Exception as e:
                log(session, project_id, f"智能体执行失败: {e}\n{traceback.format_exc()}",
                    stage, "ERROR")
                stream_bus.publish(project_id, type="stage_error", stage=stage,
                                   message=str(e))
                raise

            art_id = _save_artifact(session, project_id, stage, md, meta)
            version = prev_art.version + 1 if prev_art else 1
            # 追溯类软校验最终未达标时不丢产物，但要把缺口写进日志交评审裁决
            for w in (meta or {}).get("_warnings") or []:
                log(session, project_id, f"追溯校验警告：{w}", stage, "WARN")
            # 已落库：通知前端丢掉流式缓冲，改从 DB 拉带图表渲染的干净版本
            stream_bus.publish(project_id, type="stage_done", stage=stage,
                               version=version, artifact_id=art_id, chars=len(md))
            log(session, project_id, f"「{STAGE_TITLES[stage]}」v{version} 生成完成", stage)
            _set_status(session, project_id, "waiting_review", stage)
            log(session, project_id, f"「{STAGE_TITLES[stage]}」等待人工评审", stage)

            update = {
                "artifacts": {stage: {"id": art_id, "version": version}},
                "retry_comments": None,
                "current_stage": stage,
            }
            if stage == "requirement":
                update["srs_meta"] = meta
            elif stage == "hld":
                update["hld_meta"] = meta
            return update

    return agent_node


# ---------------- 评审门控节点 ----------------
def make_gate(stage: str):
    def gate_node(state: PipelineState) -> dict:
        from langgraph.types import interrupt

        project_id = state["project_id"]
        art = state["artifacts"][stage]

        # 暂停，等待人工评审；resume 时收到 {"approved": bool, "comments": str}
        # 注意：interrupt 之前的代码会在 resume 时重放，故 DB 写入全部放在其后
        decision = interrupt({
            "project_id": project_id, "stage": stage,
            "artifact_id": art["id"], "version": art["version"],
        })

        approved = bool(decision.get("approved"))
        comments = decision.get("comments") or ""
        with SessionLocal() as session:
            a = _get_artifact(session, art["id"])
            if a:
                a.status = "approved" if approved else "rejected"
                session.commit()
            session.add(Review(project_id=project_id, artifact_id=art["id"],
                               approved=approved, comments=comments))
            session.commit()
            log(session, project_id,
                f"评审结果：{'通过' if approved else '驳回'}"
                + (f"（意见：{comments[:200]}）" if comments else ""), stage)
            if approved:
                _set_status(session, project_id, "running", stage)
        update = {"retry_comments": comments if not approved else None}
        reset = gate_fix_rounds(stage, approved)
        if reset is not None:
            update["fix_rounds"] = reset
        return update

    return gate_node


# ---------------- 路由：通过→下一阶段，驳回→回环重跑 ----------------
def next_stage_or_end(stage: str) -> str:
    """评审门通过后的去向。工具节点不在 STAGES 里，只能在路由表里显式接上。"""
    return GATE_NEXT.get(stage, "__end__")


def make_route(stage: str):
    def route(state: PipelineState) -> str:
        return "redo" if state.get("retry_comments") else "next"
    return route


# ---------------- 工具节点公共件 ----------------
def _tail_lines(text: str, n: int = 30) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def _slim_static(report: dict) -> dict:
    """静态报告入库精简版：违规明细与函数清单必须留（追溯矩阵按函数归因），
    规则表原文不入库——它由 core.c_rules 单一事实来源随时可导出，存两份会漂移。"""
    return {k: v for k, v in (report or {}).items() if k != "rule_table"}


def _slim_exec(outcome: dict) -> dict:
    """执行结论入库精简版：原始日志已落证据目录，库里只留可判定的部分。"""
    drop = ("raw_log", "run_section", "coverage_section", "synced_files")
    slim = {k: v for k, v in (outcome or {}).items() if k not in drop}
    build = outcome.get("build") or {}
    slim["build"] = {"ok": build.get("ok"),
                     "warning_count": len(build.get("warnings") or []),
                     "errors": build.get("errors") or [],
                     "log_tail": _tail_lines(build.get("log"), 40)}
    return slim


def _deviation_block(report: dict, rounds: int) -> str:
    """超限后追加的偏差单：把「不能自动放行」的违规列成待人工裁决条目。"""
    req = [x for x in (report or {}).get("violations") or []
           if x.get("severity") == c_rules.SEV_REQUIRED]
    lines = ["", "---", "", "## 偏差单（自动整改已达上限，待人工裁决）", "",
             f"自动整改已进行 {rounds} 轮（上限 {config.MAX_FIX_ROUNDS} 轮），"
             "仍存在必查项违规。以下条目不得静默放行：", "",
             "| 序号 | 规则 | 判据 | 可否偏差 | 位置 | 说明 |",
             "|---|---|---|---|---|---|"]
    for i, x in enumerate(req, 1):
        msg = str(x.get("message", "")).replace("|", "/").replace("\n", " ")
        lines.append(f"| {i} | {x['rule_id']} | {x.get('title', '')} | "
                     f"{'可' if x.get('waivable') else '不可（安全性底线）'} | "
                     f"{x.get('file')}:{x.get('line') or '-'} | {msg} |")
    lines += ["", "> 可偏差项：人工批准后方可放行，批准意见留痕于评审记录；",
              "> 不可偏差项：只能改代码，人工亦无权放行。"]
    return "\n".join(lines)


def _problem_block(title: str, summary: str, rounds: int, detail: str = "") -> str:
    """问题报告单：闭环处置不了时留痕为交付件的一部分，而不是把失败藏起来。"""
    lines = ["", "---", "", f"## 问题报告单：{title}", "",
             f"- 自动修复轮数：{rounds}（上限 {config.MAX_FIX_ROUNDS}）",
             f"- 结论：{summary}"]
    if detail:
        lines += ["", "```text", detail.strip(), "```"]
    lines += ["", "> 本条已超出自动闭环处置能力，转人工评审门裁决。"]
    return "\n".join(lines)


def _exec_feedback(outcome: dict, decision: str, reason: str,
                   attribution: dict | None) -> str:
    """把一轮失败压成回灌给重生阶段的整改要求。

    重生不能是盲改：必须带上「责任方是谁、工具链看到了什么、要改哪一侧」。
    尤其要写死边界——判为代码缺陷时不许动测试判据，否则模型会走捷径改用例迁就实现。"""
    side = "被测代码" if decision == executor.DECISION_FIX_CODE else "测试代码"
    parts = [f"验证执行未通过，判定责任方：{side}（decision = {decision}）。"]
    if reason:
        parts.append(f"归因结论：{reason}")
    hints = [str(h) for h in ((attribution or {}).get("hint") or []) if str(h).strip()]
    if hints:
        parts.append("整改要点：\n" + "\n".join(f"- {h}" for h in hints))
    parts.append("工具链事实（原始判据，不得与之矛盾）：\n"
                 + executor.failure_brief(outcome))
    if decision == executor.DECISION_FIX_CODE:
        parts.append("只修改被测代码，不得修改任何测试用例的编号与判据；输出完整代码文件。")
    else:
        parts.append("只修改测试代码，保持用例编号与设计文档中的判据一致；输出完整测试文件。")
    return "\n\n".join(parts)


# ---------------- 工具节点：静态检查 ----------------
def static_node(state: PipelineState) -> dict:
    """静态检查：判据来自 core.c_rules（与生成器提示词同源），结论直接落库。

    必查项违规绝不静默放行：先回灌代码阶段整改（最多 MAX_FIX_ROUNDS 轮），
    超限则出问题报告 + 偏差单，转人工评审门裁决。"""
    project_id = state["project_id"]
    with SessionLocal() as session:
        _set_status(session, project_id, "running", "static")
        stream_bus.publish(project_id, type="stage_start", stage="static")
        code = _get_approved(session, project_id, "code")
        lld = _get_approved(session, project_id, "lld")
        if not code:
            raise RuntimeError("缺少代码产物，无法执行静态检查")
        files = c_files.extract_files(code.markdown)
        rounds = int((state.get("fix_rounds") or {}).get("static") or 0)
        log(session, project_id,
            f"静态检查 {len(files)} 个文件：判据 {len(c_rules.RULES)} 条，"
            f"圈复杂度上限 {config.COMPLEXITY_MAX}（第 {rounds + 1} 轮）", "static")
        report = c_static.check_files(
            files, {"complexity_max": config.COMPLEXITY_MAX},
            ((lld.meta_json or {}).get("functions") if lld else None))
        md = c_static.markdown_report(report)
        ok = bool(report.get("ok"))
        over_limit = (not ok) and rounds >= config.MAX_FIX_ROUNDS
        if over_limit:
            md += _deviation_block(report, rounds)

        prev = _get_approved(session, project_id, "static")
        version = (prev.version + 1) if prev else 1
        art_id = _save_artifact(
            session, project_id, "static", md, _slim_static(report),
            status="approved" if ok else ("pending_review" if over_limit else "rejected"))
        stream_bus.publish(project_id, type="stage_done", stage="static",
                           version=version, artifact_id=art_id, chars=len(md))

        update = {"artifacts": {"static": {"id": art_id, "version": version}},
                  "current_stage": "static"}
        if ok:
            log(session, project_id,
                f"静态检查通过：必查项 0 条，建议项 {report.get('advisory', 0)} 条",
                "static")
            # 只改代码不改测试时（exec 归因 fix_code），静态过了直接回执行验证
            update["tool_route"] = ("skip" if state.get("resume_after_code") == "exec"
                                    else "next")
            update["retry_comments"] = None
        elif over_limit:
            log(session, project_id,
                f"静态检查未通过且自动整改已达上限（{rounds} 轮），转人工裁决",
                "static", "WARN")
            _set_status(session, project_id, "waiting_review", "static")
            update["tool_route"] = "gate"
            update["retry_comments"] = None
        else:
            log(session, project_id,
                f"静态检查未通过：必查项 {report.get('required', 0)} 条，"
                f"回灌代码阶段整改（{rounds + 1}/{config.MAX_FIX_ROUNDS} 轮）",
                "static", "WARN")
            update["tool_route"] = "regen"
            update["retry_comments"] = c_static.feedback_text(report)
            update["fix_rounds"] = {"static": rounds + 1}
        return update


STATIC_ROUTE_MAP = {"next": "agent_test_impl", "regen": "agent_code",
                    "gate": "gate_static", "skip": "exec_node"}


def route_static(state: PipelineState) -> str:
    return state.get("tool_route") or "next"


gate_static = make_gate("static")

STATIC_GATE_ROUTE_MAP = {"redo": "agent_code", "next": "agent_test_impl",
                         "skip": "exec_node"}


def route_static_gate(state: PipelineState) -> str:
    """人工裁决后的去向：驳回带意见回代码阶段；通过则按原计划继续。"""
    if state.get("retry_comments"):
        return "redo"
    return "skip" if state.get("resume_after_code") == "exec" else "next"


# ---------------- 工具节点：执行验证 ----------------
def exec_node(state: PipelineState) -> dict:
    """执行验证：远端编译 → 跑测试 → 采覆盖率 → 出确定性结论。

    这一节点是整套系统的判据出口。结论不通过时先归因（工具链能判死的不问模型），
    再决定回代码还是回测试；轮数用尽则出问题报告单转人工，绝不放行也绝不空转。"""
    from agents import stage_agents

    project_id = state["project_id"]
    with SessionLocal() as session:
        _set_status(session, project_id, "running", "exec")
        stream_bus.publish(project_id, type="stage_start", stage="exec")
        code = _get_approved(session, project_id, "code")
        ti = _get_approved(session, project_id, "test_impl")
        tc = _get_approved(session, project_id, "testcase")
        if not code or not ti:
            raise RuntimeError("缺少代码或测试实现产物，无法执行验证")
        code_files = c_files.extract_files(code.markdown)
        test_files = c_files.extract_files(ti.markdown)
        ti_meta = ti.meta_json or {}
        case_ids = [c.get("id") for c in (ti_meta.get("cases") or []) if c.get("id")]
        rounds = int((state.get("fix_rounds") or {}).get("exec") or 0)
        # 版本标签同时作为远端工作区与证据目录名：代码 v2 + 测试 v1 → "2t1"，
        # 任一侧重生都会换目录，历史证据不会被覆盖。
        tag = f"{code.version}t{ti.version}"
        log(session, project_id,
            f"开始远端验证：代码 v{code.version} + 测试 v{ti.version}，"
            f"{len(code_files)} 个源文件、{len(test_files)} 个测试文件、"
            f"{len(case_ids)} 条用例（第 {rounds + 1} 轮）", "exec")

        outcome = executor.run_verification(
            runner_from_config(), project_id, tag, code_files, test_files,
            case_ids=case_ids, branch_min=config.COVERAGE_BRANCH_MIN,
            line_min=config.COVERAGE_LINE_MIN)
        evidence = executor.archive_evidence(outcome, project_id, tag)
        md = executor.markdown_report(outcome)
        if evidence:
            md += f"\n\n> 完整原始输出与结论已归档：`{evidence}`"

        ok = bool(outcome.get("ok"))
        blocked = bool(outcome.get("skipped")) or \
            outcome.get("decision") == executor.DECISION_BLOCKED
        over_limit = (not ok) and rounds >= config.MAX_FIX_ROUNDS
        decision = outcome.get("decision")
        reason = outcome.get("reason") or ""
        attribution = None
        if not ok and not blocked:
            auto = executor.auto_decision(outcome)
            if auto:
                decision, reason = auto[0], (auto[1] or reason)
                log(session, project_id,
                    f"归因（确定性判据）：{decision} — {reason}", "exec", "WARN")
            else:
                brief = executor.failure_brief(outcome)
                log(session, project_id,
                    "确定性判据不足以定责，调用归因智能体读代码与用例", "exec")
                with stream_bus.bind(project_id, "exec"):
                    att_md, attribution = stage_agents.attribute_failure(
                        brief, code_files, test_files, tc.meta_json if tc else None)
                decision = attribution.get("decision") or executor.DECISION_FIX_CODE
                reason = attribution.get("reason") or reason
                md += "\n\n---\n\n" + att_md
                log(session, project_id,
                    f"归因（智能体）：{decision} — {reason}", "exec", "WARN")
        if over_limit and not blocked:
            md += _problem_block("验证执行未通过", reason or outcome.get("reason") or "",
                                 rounds, executor.failure_brief(outcome))

        meta = _slim_exec(outcome)
        meta["evidence"] = evidence
        if attribution:
            meta["attribution"] = {**attribution, "source": "agent"}
        elif decision and not ok:
            meta["attribution"] = {"decision": decision, "reason": reason,
                                   "source": "deterministic"}
        need_gate = blocked or over_limit
        prev = _get_approved(session, project_id, "exec")
        version = (prev.version + 1) if prev else 1
        art_id = _save_artifact(
            session, project_id, "exec", md, meta,
            status="approved" if ok else ("pending_review" if need_gate else "rejected"))
        stream_bus.publish(project_id, type="stage_done", stage="exec",
                           version=version, artifact_id=art_id, chars=len(md))

        update = {"artifacts": {"exec": {"id": art_id, "version": version}},
                  "current_stage": "exec", "resume_after_code": None,
                  "retry_comments": None}
        if attribution:
            update["attribution"] = meta["attribution"]
        elif decision and not ok:
            update["attribution"] = meta["attribution"]

        tests = outcome.get("tests") or {}
        cov = (outcome.get("coverage") or {}).get("totals") or {}
        if ok:
            log(session, project_id,
                f"验证执行通过：用例 {tests.get('passed')}/{tests.get('expected') or tests.get('total')} 全过，"
                f"行覆盖 {cov.get('line_pct')}%，分支覆盖 {cov.get('branch_pct')}%"
                f"（门限 {config.COVERAGE_BRANCH_MIN}%），耗时 {outcome.get('duration_s')}s",
                "exec")
            update["tool_route"] = "next"
        elif blocked:
            log(session, project_id,
                f"验证未执行：{outcome.get('reason')}，转人工裁决", "exec", "WARN")
            _set_status(session, project_id, "waiting_review", "exec")
            update["tool_route"] = "gate"
        elif over_limit:
            log(session, project_id,
                f"验证执行未通过且自动修复已达上限（{rounds} 轮），出问题报告单转人工裁决",
                "exec", "WARN")
            _set_status(session, project_id, "waiting_review", "exec")
            update["tool_route"] = "gate"
        else:
            fix_test = decision == executor.DECISION_FIX_TEST
            back = "测试实现" if fix_test else "代码"
            log(session, project_id,
                f"验证执行未通过（{reason}），回「{back}」阶段重生"
                f"（{rounds + 1}/{config.MAX_FIX_ROUNDS} 轮）", "exec", "WARN")
            update["tool_route"] = "fix_test" if fix_test else "fix_code"
            update["retry_comments"] = _exec_feedback(outcome, decision, reason,
                                                      attribution)
            update["fix_rounds"] = {"exec": rounds + 1}
            if not fix_test:
                # 只改代码：静态检查通过后直接回本节点，不重生测试、不过测试评审门
                update["resume_after_code"] = "exec"
        return update


EXEC_ROUTE_MAP = {"next": "report_node", "fix_code": "agent_code",
                  "fix_test": "agent_test_impl", "gate": "gate_exec"}


def route_exec(state: PipelineState) -> str:
    return state.get("tool_route") or "next"


gate_exec = make_gate("exec")

EXEC_GATE_ROUTE_MAP = {"redo_code": "agent_code", "redo_test": "agent_test_impl",
                       "next": "report_node"}


def route_exec_gate(state: PipelineState) -> str:
    """人工裁决：通过则带着问题报告单去装配交付件；驳回则按归因责任方回对应阶段。"""
    if not state.get("retry_comments"):
        return "next"
    decision = (state.get("attribution") or {}).get("decision")
    return "redo_test" if decision == executor.DECISION_FIX_TEST else "redo_code"


# ---------------- 工具节点：交付件装配 ----------------
def report_node(state: PipelineState) -> dict:
    """装配 GJB 438B 风格交付件：软件测评报告 + 追溯摘要 + 问题报告单 + 证据清单。

    报告只汇总已落库的产物，不再调 LLM——交付件里的每个结论都必须能回溯到
    某一份产物或某一条工具输出，模型现编的话不能进交付件。"""
    from exporters.report_exporter import assemble_report, report_markdown

    project_id = state["project_id"]
    with SessionLocal() as session:
        _set_status(session, project_id, "running", "report")
        stream_bus.publish(project_id, type="stage_start", stage="report")
        arts, versions = {}, {}
        for st in DISPLAY_STAGES:
            a = _get_approved(session, project_id, st)
            if not a:
                continue
            arts[st] = {"markdown": a.markdown, "meta": a.meta_json or {},
                        "version": a.version, "status": a.status}
            versions[st] = a.version
        # 问题报告单要能追溯到「曾经失败过的那几轮」，只给最新版就会把过程抹掉，
        # 归零材料里最关键的恰恰是问题清单与处置过程。
        history = {}
        for st in ("static", "exec"):
            runs = (session.query(StageArtifact)
                    .filter_by(project_id=project_id, stage=st)
                    .order_by(StageArtifact.version.asc()).all())
            history[st] = [{"version": r.version, "status": r.status,
                            "meta": r.meta_json or {}} for r in runs]
        proj = session.get(Project, project_id)
        name = (proj.name if proj else "") or f"项目 {project_id}"
        log(session, project_id,
            f"装配交付件：已收集 {len(arts)} 个阶段产物"
            f"（{', '.join(f'{k} v{v}' for k, v in versions.items())}）",
            "report")
        data = assemble_report(arts, name, versions, history=history,
                               coverage_min=config.COVERAGE_BRANCH_MIN)
        data["thresholds"]["max_fix_rounds"] = config.MAX_FIX_ROUNDS
        md = report_markdown(data)
        prev = _get_approved(session, project_id, "report")
        version = (prev.version + 1) if prev else 1
        art_id = _save_artifact(session, project_id, "report", md, data,
                                approved_status=True)
        stream_bus.publish(project_id, type="stage_done", stage="report",
                           version=version, artifact_id=art_id, chars=len(md))
        verdict = data.get("conclusion", {}).get("text", "")
        log(session, project_id,
            f"「{STAGE_TITLES['report']}」v{version} 装配完成：{verdict}", "report")
        return {"artifacts": {"report": {"id": art_id, "version": version}},
                "current_stage": "report", "tool_route": None}
