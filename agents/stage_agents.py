"""阶段 2-5 智能体：需求分析 / 概要设计 / 详细设计 / 测试用例。
统一输出约定：Markdown 文档 + 末尾 ```json 元数据块（测试用例为纯 JSON）。"""
from agents.base_agent import run_agent

COMMON_RULES = """
输出约定（必须严格遵守）：
1. 全部文档内容使用 Markdown 格式；所有图表必须使用 Mermaid 语法（```mermaid 代码块）。
2. 在 Markdown 文档的最后，追加一个 ```json 代码块作为元数据，供系统校验与下游引用。
3. 内容必须基于给定的输入材料，不得编造输入中不存在的核心需求；对材料未覆盖之处应明确指出。
4. 默认目标语言为 C 语言（嵌入式/裸机风格）：使用 struct、typedef、函数指针、模块划分等 C 范式，
   禁止使用类、对象、继承、多态等面向对象概念；图表中不要出现 classDiagram。
"""

RETRY_RULES = """
4. 如果是修订任务：输入中会给出上一版产物与评审意见，你必须逐条回应评审意见，
   并在文档开头添加「修订说明」章节，逐条列出意见及对应修改。
"""

# ---------------- 需求分析 ----------------
REQUIREMENT_SYSTEM = """你是一名资深需求分析师，擅长将原始需求材料整理为规范的软件需求规格说明书（SRS）。""" + COMMON_RULES

REQUIREMENT_PROMPT = """请基于以下《结构化原始数据》撰写《软件需求规格说明书》。

要求包含章节：
1. 引言
2. 功能需求清单：表格（编号 FR-xxx、需求描述、优先级 P0/P1/P2）
3. 用户故事：每条格式「作为【角色】，我想要【能力】，以便【价值】」，编号 US-xxx
4. 业务逻辑：从输入材料提炼的关键业务规则，逐条编号
5. 模糊点与风险项：表格（类型、编号 AMB/RISK-xxx、描述、严重度 高/中/低）

末尾 ```json 元数据格式：
{{"functional_requirements": [{{"id","desc","priority"}}],
 "ambiguities": [{{"id","desc","severity"}}],
 "risks": [{{"id","desc","severity"}}]}}

输入材料：
{input}
{retry_block}"""

# ---------------- 概要设计 ----------------
HLD_SYSTEM = """你是一名资深系统架构师，擅长基于需求规格说明书产出概要设计。""" + COMMON_RULES

HLD_PROMPT = """请基于以下《软件需求规格说明书》撰写《概要设计说明书》。

要求包含章节：
1. 系统模块划分：表格（模块、职责、依赖）
2. 技术架构：说明技术选型，并给出 Mermaid `graph TD` 架构图
3. 核心业务流程：至少一个 Mermaid `flowchart` 流程图
4. 数据库设计：核心表 DDL（```sql），表间关系说明
5. 核心 API：表格（方法、路径、说明）

末尾 ```json 元数据格式：
{{"modules": ["..."], "tables": ["..."], "apis": [{{"method","path","desc"}}]}}

输入材料：
{input}
{retry_block}"""

# ---------------- 详细设计 ----------------
LLD_SYSTEM = """你是一名资深软件设计师，擅长基于概要设计逐模块细化详细设计。""" + COMMON_RULES

LLD_PROMPT = """请基于以下《概要设计说明书》（及其需求背景）撰写《详细设计说明书》。
目标语言为 C（无类概念，禁止面向对象设计）。

要求包含章节：
1. 各核心模块数据结构设计：Mermaid `flowchart LR` 模块结构图（节点为模块/struct），
   并用 ```c 代码块给出关键 struct / typedef / 枚举定义及字段注释
2. 接口定义：逐函数给出 C 函数签名、参数说明、返回值、异常情况
3. 核心业务序列图：至少一个 Mermaid `sequenceDiagram`（参与者为模块/函数，消息为函数调用）
4. 关键业务流程细化说明（含状态流转、内存/资源管理等 C 相关要点）

末尾 ```json 元数据格式：
{{"data_structures": ["..."], "functions": [{{"name","sig","desc"}}]}}

输入材料（概要设计为主，需求规格作背景参考）：
{input}
{retry_block}"""

# ---------------- 测试用例 ----------------
TESTCASE_SYSTEM = """你是一名资深测试工程师，擅长根据需求与设计文档设计全面的结构化测试用例。""" + COMMON_RULES + """
5. 最终只输出一个 ```json 代码块（不要输出 Markdown 文档正文），系统会自动渲染。"""

TESTCASE_PROMPT = """请根据以下需求与设计材料生成结构化测试用例。

覆盖维度要求（每个维度都必须有对应用例）：
- 功能：核心业务流程的正向用例
- 边界：数值边界、长度边界、临界条件
- 异常：非法输入、依赖失败、并发冲突
- 场景：端到端组合场景、涉及多条业务规则联动的场景

每个用例字段：
{{"id": "TC-xxx", "module": "所属模块", "title": "用例标题", "type": "功能|边界|异常|场景",
 "priority": "P0|P1|P2", "preconditions": "前置条件",
 "steps": ["步骤1", "步骤2"], "expected": "预期结果"}}

输出格式：只输出一个 ```json 代码块：{{"testcases": [ ... ]}}

输入材料（需求规格说明书为主）：
{input}
{retry_block}"""


def _retry_block(retry_comments: str | None, previous_markdown: str | None) -> str:
    if not retry_comments:
        return ""
    return f"""
---
【修订任务】
评审意见（必须逐条回应）：
{retry_comments}

上一版产物（请在此基础上修订）：
{previous_markdown or "（无）"}
"""


def analyze_requirements(structured_json: str,
                         retry_comments: str | None = None,
                         previous_markdown: str | None = None,
                         progress_cb=None) -> tuple[str, dict]:
    def validate(meta):
        frs = meta.get("functional_requirements")
        if not isinstance(frs, list) or not frs:
            return "functional_requirements 缺失或为空"
        for fr in frs:
            if not all(k in fr for k in ("id", "desc", "priority")):
                return "functional_requirements 条目缺少 id/desc/priority 字段"
        return None

    return run_agent(
        role="requirement",
        system_prompt=REQUIREMENT_SYSTEM + RETRY_RULES,
        user_prompt=REQUIREMENT_PROMPT.format(
            input=structured_json,
            retry_block=_retry_block(retry_comments, previous_markdown)),
        validator=validate,
        progress_cb=progress_cb,
    )


def design_hld(srs_markdown: str, srs_meta: dict,
               retry_comments: str | None = None,
               previous_markdown: str | None = None,
               progress_cb=None) -> tuple[str, dict]:
    def validate(meta):
        if not meta.get("modules"):
            return "modules 缺失或为空"
        if not meta.get("tables"):
            return "tables 缺失或为空"
        return None

    import json as _json
    return run_agent(
        role="hld",
        system_prompt=HLD_SYSTEM + RETRY_RULES,
        user_prompt=HLD_PROMPT.format(
            input=f"《软件需求规格说明书》全文：\n{srs_markdown}\n\n需求元数据：\n{_json.dumps(srs_meta, ensure_ascii=False)}",
            retry_block=_retry_block(retry_comments, previous_markdown)),
        validator=validate,
        progress_cb=progress_cb,
    )


def design_lld(hld_markdown: str, hld_meta: dict, srs_markdown: str,
               retry_comments: str | None = None,
               previous_markdown: str | None = None,
               progress_cb=None) -> tuple[str, dict]:
    def validate(meta):
        if not meta.get("data_structures"):
            return "data_structures 缺失或为空"
        if not meta.get("functions"):
            return "functions 缺失或为空"
        return None

    import json as _json
    return run_agent(
        role="lld",
        system_prompt=LLD_SYSTEM + RETRY_RULES,
        user_prompt=LLD_PROMPT.format(
            input=f"《概要设计说明书》全文：\n{hld_markdown}\n\n概要设计元数据：\n{_json.dumps(hld_meta, ensure_ascii=False)}\n\n需求背景（SRS 摘录）：\n{srs_markdown[:3000]}",
            retry_block=_retry_block(retry_comments, previous_markdown)),
        validator=validate,
        progress_cb=progress_cb,
    )


def generate_testcases(srs_markdown: str, srs_meta: dict,
                       retry_comments: str | None = None,
                       previous_markdown: str | None = None,
                       progress_cb=None) -> tuple[str, dict]:
    def validate(meta):
        cases = meta.get("testcases")
        if not isinstance(cases, list) or not cases:
            return "testcases 缺失或为空"
        required = ("id", "preconditions", "steps", "expected")
        for c in cases:
            for k in required:
                if k not in c:
                    return f"用例 {c.get('id', '?')} 缺少字段 {k}"
        types = {c.get("type") for c in cases}
        if not {"功能", "边界", "异常", "场景"}.issubset(types):
            return f"用例类型覆盖不全，当前类型：{types}"
        return None

    import json as _json
    markdown, meta = run_agent(
        role="testcase",
        system_prompt=TESTCASE_SYSTEM + RETRY_RULES,
        user_prompt=TESTCASE_PROMPT.format(
            input=f"《软件需求规格说明书》全文：\n{srs_markdown}\n\n需求元数据：\n{_json.dumps(srs_meta, ensure_ascii=False)}",
            retry_block=_retry_block(retry_comments, previous_markdown)),
        validator=validate,
        expect_json_only=True,
        progress_cb=progress_cb,
    )
    # 渲染 Markdown 版本供评审展示
    lines = ["# 测试用例设计", "",
             f"> 共 {len(meta['testcases'])} 条用例，覆盖功能/边界/异常/场景四个维度。", ""]
    lines += ["| 编号 | 模块 | 标题 | 类型 | 优先级 |", "|---|---|---|---|---|"]
    for c in meta["testcases"]:
        lines.append(f"| {c['id']} | {c.get('module','-')} | {c.get('title','-')} | "
                     f"{c.get('type','-')} | {c.get('priority','-')} |")
    for c in meta["testcases"]:
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(c.get("steps", []), 1))
        lines += ["", f"## {c['id']} {c.get('title','')}",
                  f"- **类型**：{c.get('type','-')}　**优先级**：{c.get('priority','-')}",
                  f"- **前置条件**：{c.get('preconditions','-')}",
                  "- **步骤**：", steps,
                  f"- **预期结果**：{c.get('expected','-')}"]
    return "\n".join(lines), meta
