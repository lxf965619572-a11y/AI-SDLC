"""阶段 2-5 智能体：需求分析 / 概要设计 / 详细设计 / 测试用例。
统一输出约定：Markdown 文档 + 末尾 ```json 元数据块（测试用例为纯 JSON）。"""
import json

from agents.base_agent import run_agent
from core import trace

COMMON_RULES = """
输出约定（必须严格遵守）：
1. 全部文档内容使用 Markdown 格式；所有图表必须使用 Mermaid 语法（```mermaid 代码块）。
2. 在 Markdown 文档的最后，追加一个 ```json 代码块作为元数据，供系统校验与下游引用。
3. 内容必须基于给定的输入材料，不得编造输入中不存在的核心需求；对材料未覆盖之处应明确指出。
"""

# 目标语言约束只适用于设计类阶段（概要/详细）：需求分析描述的是做什么，
# 与实现语言无关，绑上 C 范式会让 SRS 出现无意义的语言级约束。
TARGET_LANG_RULES = """
4. 默认目标语言为 C 语言（嵌入式/裸机风格）：使用 struct、typedef、函数指针、模块划分等 C 范式，
   禁止使用类、对象、继承、多态等面向对象概念；图表中不要出现 classDiagram。
"""

DESIGN_RULES = COMMON_RULES + TARGET_LANG_RULES

RETRY_RULES = """
修订任务附加要求：如果本次是修订任务，输入中会给出上一版产物与评审意见，
你必须逐条回应评审意见，并在文档开头添加「修订说明」章节，逐条列出意见及对应修改。
"""

# ---------------- 需求分析 ----------------
REQUIREMENT_SYSTEM = """你是一名资深需求分析师，擅长将原始需求材料整理为规范的软件需求规格说明书（SRS）。""" + COMMON_RULES

REQUIREMENT_PROMPT = """请基于以下《结构化原始数据》撰写《软件需求规格说明书》。

要求包含章节：
1. 引言
2. 功能需求清单：表格（编号 FR-xxx、需求描述、优先级 P0/P1/P2、来源编号）
3. 用户故事：每条格式「作为【角色】，我想要【能力】，以便【价值】」，编号 US-xxx
4. 业务逻辑：从输入材料提炼的关键业务规则，逐条编号
5. 模糊点与风险项：表格（类型、编号 AMB/RISK-xxx、描述、严重度 高/中/低）

追溯要求：每条功能需求必须标注它来自输入材料中的哪些素材编号
（OBJ-xxx 业务对象 / RULE-xxx 业务规则 / FLOW-xxx 业务流程）；
只能使用输入材料里出现过的编号，不得编造，一条需求可以对应多个来源。

末尾 ```json 元数据格式：
{{"functional_requirements": [{{"id","desc","priority","derived_from":["RULE-001"]}}],
 "ambiguities": [{{"id","desc","severity"}}],
 "risks": [{{"id","desc","severity"}}]}}

输入材料：
{input}
{retry_block}"""

# ---------------- 概要设计 ----------------
HLD_SYSTEM = """你是一名资深系统架构师，擅长基于需求规格说明书产出概要设计。""" + DESIGN_RULES

HLD_PROMPT = """请基于以下《软件需求规格说明书》撰写《概要设计说明书》。

要求包含章节：
1. 系统模块划分：表格（模块、职责、依赖）
2. 技术架构：说明技术选型，并给出 Mermaid `graph TD` 架构图
3. 核心业务流程：至少一个 Mermaid `flowchart` 流程图
4. 数据库设计：若系统存在持久化数据，给出核心表 DDL（```sql）与表间关系说明；
   若目标系统没有数据库（如嵌入式裸机 C 项目），本章明确写「本系统无数据库设计」，
   并说明数据如何以 struct / 静态存储 / 非易失存储组织。
   不要为了凑齐格式而编造数据表——这种情况元数据里的 tables 给空数组即可。
5. 核心 API：表格（方法、路径、说明）
6. 需求承接：表格（模块、承接的需求编号 FR-xxx、说明），每条 P0 需求都必须有模块承接

末尾 ```json 元数据格式：
{{"modules": ["..."], "tables": ["..."], "apis": [{{"method","path","desc"}}],
 "derived_from": {{"模块名": ["FR-001","FR-003"]}}}}
（tables 在无数据库时为空数组；modules 与 apis 不可为空；
 derived_from 的键必须与 modules 里的模块名完全一致，值为该模块承接的需求编号）

输入材料：
{input}
{retry_block}"""

# ---------------- 详细设计 ----------------
LLD_SYSTEM = """你是一名资深软件设计师，擅长基于概要设计逐模块细化详细设计。""" + DESIGN_RULES

LLD_PROMPT = """请基于以下《概要设计说明书》（及其需求背景）撰写《详细设计说明书》。
目标语言为 C（无类概念，禁止面向对象设计）。

要求包含章节：
1. 各核心模块数据结构设计：Mermaid `flowchart LR` 模块结构图（节点为模块/struct），
   并用 ```c 代码块给出关键 struct / typedef / 枚举定义及字段注释
2. 接口定义：逐函数给出 C 函数签名、参数说明、返回值、异常情况
3. 核心业务序列图：至少一个 Mermaid `sequenceDiagram`（参与者为模块/函数，消息为函数调用）
4. 关键业务流程细化说明（含状态流转、内存/资源管理等 C 相关要点）

末尾 ```json 元数据格式：
{{"data_structures": ["..."], "functions": [{{"name","sig","desc"}}],
 "derived_from": {{"函数名": ["FR-001"]}}}}
（derived_from 的键为 functions 里的函数名，值填该函数实现的需求编号 FR-xxx；
 确实无法直接对应需求时，可填它所属的模块名，系统会按概要设计的承接关系展开）

输入材料（概要设计为主，需求规格作背景参考）：
{input}
{retry_block}"""

# ---------------- 测试用例 ----------------
TESTCASE_SYSTEM = """你是一名资深测试工程师，擅长根据需求与设计文档设计全面的结构化测试用例。""" + COMMON_RULES + """
4. 最终只输出一个 ```json 代码块（不要输出 Markdown 文档正文），系统会自动渲染。"""

TESTCASE_PROMPT = """请根据以下需求与设计材料生成结构化测试用例。

覆盖维度要求（每个维度都必须有对应用例）：
- 功能：核心业务流程的正向用例
- 边界：数值边界、长度边界、临界条件
- 异常：非法输入、依赖失败、并发冲突
- 场景：端到端组合场景、涉及多条业务规则联动的场景

每个用例字段：
{{"id": "TC-xxx", "module": "所属模块", "title": "用例标题", "type": "功能|边界|异常|场景",
  "priority": "P0|P1|P2", "preconditions": "前置条件",
  "steps": ["步骤1", "步骤2"], "expected": "预期结果", "fr_ids": ["FR-001"]}}

追溯要求：每条用例必须用 fr_ids 标注它验证哪些需求编号（取自需求元数据，不得编造）；
每条 P0 需求至少要被一条用例覆盖。

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


def _source_ids(structured_json: str) -> set[str]:
    """解析素材编号集合（OBJ/RULE/FLOW-xxx），供 SRS 追溯校验使用。

    老项目的结构化数据没有编号，这里按 assign_ids 的同一规则补算，
    保证校验口径与前端矩阵一致；解析失败则返回空集（不做追溯追责）。
    """
    try:
        data = json.loads(structured_json)
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    keys = ("objects", "rules", "flows")
    ids = {str(it["id"]) for k in keys for it in (data.get(k) or [])
           if isinstance(it, dict) and it.get("id")}
    if not ids:
        data = trace.assign_ids(data)
        ids = {it["id"] for k in keys for it in (data.get(k) or [])}
    return ids


def analyze_requirements(structured_json: str,
                         retry_comments: str | None = None,
                         previous_markdown: str | None = None,
                         progress_cb=None) -> tuple[str, dict]:
    valid_srcs = _source_ids(structured_json)

    def validate(meta):
        frs = meta.get("functional_requirements")
        if not isinstance(frs, list) or not frs:
            return "functional_requirements 缺失或为空"
        for fr in frs:
            if not all(k in fr for k in ("id", "desc", "priority")):
                return "functional_requirements 条目缺少 id/desc/priority 字段"
        return None

    def soft_validate(meta):
        if not valid_srcs:
            return None
        missing = [str(fr.get("id", "?")) for fr in meta["functional_requirements"]
                   if isinstance(fr, dict)
                   and not (set(trace.norm_refs(fr.get("derived_from"), "src")) & valid_srcs)]
        if missing:
            return (f"以下需求缺少可追溯的素材来源编号（derived_from 应取自 "
                    f"OBJ/RULE/FLOW-xxx）：{missing}")
        return None

    markdown, meta = run_agent(
        role="requirement",
        system_prompt=REQUIREMENT_SYSTEM + RETRY_RULES,
        user_prompt=REQUIREMENT_PROMPT.format(
            input=structured_json,
            retry_block=_retry_block(retry_comments, previous_markdown)),
        validator=validate,
        soft_validator=soft_validate,
        progress_cb=progress_cb,
    )
    for fr in meta.get("functional_requirements") or []:
        if isinstance(fr, dict):
            fr["id"] = trace.norm_fr(fr.get("id")) or fr.get("id")
            refs = trace.norm_refs(fr.get("derived_from"), "src")
            if valid_srcs:
                # 编造的素材编号直接丢弃：宁可显示断链，也不要假追溯
                refs = [r for r in refs if r in valid_srcs]
            fr["derived_from"] = refs
    return markdown, meta


def design_hld(srs_markdown: str, srs_meta: dict,
               retry_comments: str | None = None,
               previous_markdown: str | None = None,
               progress_cb=None) -> tuple[str, dict]:
    frs = trace.collect_frs(srs_meta)
    p0 = [f["id"] for f in frs if f["priority"] == "P0"]

    def validate(meta):
        if not meta.get("modules"):
            return "modules 缺失或为空"
        # tables 可为空：无数据库的系统（嵌入式裸机 C）本就无表可填，
        # 强制非空会逼模型编造数据表，直接违反「不得编造」约定。
        if not meta.get("tables") and not meta.get("apis"):
            return "tables 与 apis 不可同时为空"
        return None

    def soft_validate(meta):
        if not frs:
            return None          # 上游没有需求清单，无从追溯
        modules = [str(m).strip() for m in meta.get("modules") or []]
        mapped = trace.norm_ref_map(meta.get("derived_from"),
                                    valid_keys=set(modules), keep_indirect=False)
        if not mapped:
            return "缺少 derived_from（模块名 -> 承接的需求编号数组）"
        orphan = [m for m in modules if not mapped.get(m)]
        if orphan:
            return f"以下模块未标注承接的需求编号：{orphan}"
        if p0:
            miss = trace.uncovered(p0, trace.reverse_index(mapped))
            if miss:
                return f"以下 P0 需求没有任何模块承接：{miss}"
        return None

    import json as _json
    markdown, meta = run_agent(
        role="hld",
        system_prompt=HLD_SYSTEM + RETRY_RULES,
        user_prompt=HLD_PROMPT.format(
            input=f"《软件需求规格说明书》全文：\n{srs_markdown}\n\n需求元数据：\n{_json.dumps(srs_meta, ensure_ascii=False)}",
            retry_block=_retry_block(retry_comments, previous_markdown)),
        validator=validate,
        soft_validator=soft_validate,
        progress_cb=progress_cb,
    )
    meta["derived_from"] = trace.norm_ref_map(
        meta.get("derived_from"),
        valid_keys={str(m).strip() for m in meta.get("modules") or []},
        keep_indirect=False)
    known_frs = {f["id"] for f in frs}
    for name, refs in meta["derived_from"].items():
        meta["derived_from"][name] = trace.filter_refs(refs, valid_frs=known_frs)
    return markdown, meta


def design_lld(hld_markdown: str, hld_meta: dict, srs_markdown: str,
               srs_meta: dict | None = None,
               retry_comments: str | None = None,
               previous_markdown: str | None = None,
               progress_cb=None) -> tuple[str, dict]:
    frs = trace.collect_frs(srs_meta)

    def validate(meta):
        if not meta.get("data_structures"):
            return "data_structures 缺失或为空"
        if not meta.get("functions"):
            return "functions 缺失或为空"
        return None

    def soft_validate(meta):
        if not frs:
            return None
        funcs = [str(f.get("name", "")).strip()
                 for f in meta.get("functions") or [] if isinstance(f, dict)]
        funcs = [f for f in funcs if f]
        if not funcs:
            return None
        mapped = trace.norm_ref_map(meta.get("derived_from"))
        orphan = [f for f in funcs if not mapped.get(f)]
        if orphan:
            return f"以下函数未标注对应需求（derived_from 缺键）：{orphan}"
        return None

    import json as _json
    markdown, meta = run_agent(
        role="lld",
        system_prompt=LLD_SYSTEM + RETRY_RULES,
        user_prompt=LLD_PROMPT.format(
            input=(f"《概要设计说明书》全文：\n{hld_markdown}\n\n"
                   f"概要设计元数据：\n{_json.dumps(hld_meta, ensure_ascii=False)}\n\n"
                   f"需求清单（含编号与优先级，derived_from 只能引用这里的编号）：\n"
                   f"{_json.dumps((srs_meta or {}).get('functional_requirements') or [], ensure_ascii=False)}\n\n"
                   f"需求背景（SRS 摘录）：\n{srs_markdown[:3000]}"),
            retry_block=_retry_block(retry_comments, previous_markdown)),
        validator=validate,
        soft_validator=soft_validate,
        progress_cb=progress_cb,
    )
    meta["derived_from"] = {
        name: trace.filter_refs(refs,
                                valid_frs={f["id"] for f in frs},
                                valid_modules={str(m).strip()
                                               for m in (hld_meta or {}).get("modules") or []})
        for name, refs in trace.norm_ref_map(meta.get("derived_from")).items()}
    return markdown, meta


def generate_testcases(srs_markdown: str, srs_meta: dict,
                       retry_comments: str | None = None,
                       previous_markdown: str | None = None,
                       progress_cb=None) -> tuple[str, dict]:
    frs = trace.collect_frs(srs_meta)
    p0 = [f["id"] for f in frs if f["priority"] == "P0"]

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

    def soft_validate(meta):
        if not frs:
            return None
        cases = [c for c in meta.get("testcases") or [] if isinstance(c, dict)]
        orphan = [str(c.get("id", "?")) for c in cases
                  if not trace.norm_refs(c.get("fr_ids"), "fr")]
        if orphan:
            return f"以下用例未标注验证的需求编号 fr_ids：{orphan}"
        if p0:
            by_fr = {}
            for c in cases:
                for fid in trace.norm_refs(c.get("fr_ids"), "fr"):
                    by_fr.setdefault(fid, []).append(c.get("id"))
            miss = trace.uncovered(p0, by_fr)
            if miss:
                return f"以下 P0 需求没有任何用例覆盖：{miss}"
        return None

    import json as _json
    markdown, meta = run_agent(
        role="testcase",
        system_prompt=TESTCASE_SYSTEM + RETRY_RULES,
        user_prompt=TESTCASE_PROMPT.format(
            input=f"《软件需求规格说明书》全文：\n{srs_markdown}\n\n需求元数据：\n{_json.dumps(srs_meta, ensure_ascii=False)}",
            retry_block=_retry_block(retry_comments, previous_markdown)),
        validator=validate,
        soft_validator=soft_validate,
        expect_json_only=True,
        progress_cb=progress_cb,
    )
    # 规范化：fr_ids 统一成 FR-001 形式，非法编号丢弃
    meta["testcases"] = trace.norm_case_refs(meta.get("testcases"))
    cases = meta["testcases"]

    covered: set[str] = set()
    for c in cases:
        covered.update(c.get("fr_ids") or [])
    gaps = [f for f in frs if f["id"] not in covered]
    p0_gaps = [f["id"] for f in gaps if f["priority"] == "P0"]

    # 渲染 Markdown 版本供评审展示
    lines = ["# 测试用例设计", ""]
    for w in meta.get("_warnings") or []:
        lines += [f"> ⚠️ 追溯校验未通过：{w}", ""]
    cov_txt = f"，P0 需求覆盖 {len(p0) - len(p0_gaps)}/{len(p0)}" if p0 else ""
    lines += [f"> 共 {len(cases)} 条用例，覆盖功能/边界/异常/场景四个维度{cov_txt}。", ""]
    lines += ["| 编号 | 模块 | 标题 | 类型 | 优先级 | 验证需求 |",
              "|---|---|---|---|---|---|"]
    for c in cases:
        lines.append(f"| {c['id']} | {c.get('module','-')} | {c.get('title','-')} | "
                      f"{c.get('type','-')} | {c.get('priority','-')} | "
                      f"{'、'.join(c.get('fr_ids') or []) or '-'} |")
    for c in cases:
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(c.get("steps", []), 1))
        lines += ["", f"## {c['id']} {c.get('title','')}",
                  f"- **类型**：{c.get('type','-')}　**优先级**：{c.get('priority','-')}",
                  f"- **验证需求**：{'、'.join(c.get('fr_ids') or []) or '未标注'}",
                  f"- **前置条件**：{c.get('preconditions','-')}",
                  "- **步骤**：", steps,
                  f"- **预期结果**：{c.get('expected','-')}"]
    if gaps:
        lines += ["", "## 需求覆盖缺口", "",
                  "> 以下需求当前没有任何用例覆盖，评审时请确认是遗漏还是确无需验证。", "",
                  "| 编号 | 优先级 | 需求描述 |", "|---|---|---|"]
        for f in gaps:
            lines.append(f"| {f['id']} | {f['priority']} | {f['desc'][:60]} |")
    return "\n".join(lines), meta
