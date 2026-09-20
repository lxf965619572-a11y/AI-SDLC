"""各阶段智能体：需求分析 / 概要设计 / 详细设计 / 测试用例 / 代码实现 / 测试实现 / 失败归因。
统一输出约定：Markdown 文档 + 末尾 ```json 元数据块（测试用例与归因为纯 JSON）。

代码与测试实现两个阶段与前面四个有一个本质区别：判据不在文档里，而在工具链上。
所以它们的硬校验器不是「字段齐不齐」，而是「远端 gcc 能不能编过、能不能链起来」——
模型说写完了不算数，编译器说能编才算数。"""
import json
import re

from agents.base_agent import run_agent

import config
from core import c_files, c_rules, trace
from verification import buildkit, parsers

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


# ================= 代码实现 / 测试实现 / 失败归因 =================
# 这三个智能体与前四个的差别在于判据来源：前四个的判据是「字段齐不齐、追溯连不连」，
# 这三个的判据是工具链——编译器、链接器、静态检查器、gcov。
# 所以它们的硬校验器直接拿生成物去远端跑一遍，跑不过就把工具输出原样回灌。

CODE_RULES = """
输出约定（必须严格遵守）：
1. 只输出被测模块自身的源码，不要输出测试代码（测试由后续独立阶段编写）。
2. 每个源文件写成「## 文件 <相对路径>」标题，紧跟一个 ```c 围栏代码块给出完整文件内容；
   路径只能用 include/*.h 与 src/*.c，不要写绝对路径、./ 前缀或反斜杠。
3. 源码只放在围栏代码块里，不要为了讲解把同一个文件拆成多段代码块。
4. 在 Markdown 最后追加一个 ```json 代码块作为元数据。源码不要写进 JSON——
   C 代码里的引号与反斜杠转义极易出错，系统只从围栏代码块取文件内容。
"""

CODE_SYSTEM = ("""你是一名资深航天嵌入式 C 工程师，负责把《详细设计说明书》落地为可编译、可验证的 C99 源码。
你写的每一行都会进入交付件，并被静态检查器与测试覆盖率复核，因此判据优先于个人风格偏好。"""
               + CODE_RULES)

CODE_PROMPT = """请把下面的《详细设计说明书》实现为 C99 源码。

## 编码子集（硬性判据，与静态检查器同源，违反即不通过）
{rules}

## 目录与文件约定
- 头文件放 `include/*.h`：struct / enum / 宏 / 函数原型，并加 include guard。
- 实现放 `src/*.c`：函数定义；仅模块内部使用的辅助函数必须加 `static`。
- 不要输出 `tests/` 下的任何文件。
- 模块状态一律收敛到调用方传入的上下文结构体指针，不使用文件作用域可写变量。
- 所有对外函数必须做入参防护（空指针、长度越界、非法取值），
  防护分支返回明确的错误码，不得崩溃、不得返回未初始化的值。

## 质量要求
- 以 `gcc -std=c99 -Wall -Wextra` 零告警为目标：留意有符号/无符号比较、
  未使用的形参（用 `(void)x;` 消化）、隐式收窄（显式强转）。
- 严格实现详细设计 `functions` 清单里的每一个函数，函数名、返回类型、
  参数类型与顺序必须与清单给出的签名完全一致，不得增删对外接口。

## 详细设计说明书
{lld}

## 函数清单（必须逐个实现，签名以此为准）
{functions}

## 需要落实的功能需求（derived_from 只能引用这里的编号）
{frs}

末尾 ```json 元数据格式：
{{"module": "模块名", "files": ["include/comm.h", "src/comm.c"],
 "derived_from": {{"函数名": ["FR-001", "FR-002"]}}}}
（files 列出本次输出的全部相对路径；derived_from 的键必须是函数清单里的函数名，
 值为该函数实现的需求编号 FR-xxx，只填直接实现的需求）
{retry_block}"""


TEST_IMPL_RULES = """
输出约定（必须严格遵守）：
1. 只在 tests/ 下输出 .c 文件，不得输出或修改 src/ 与 include/ 的任何文件。
2. 必须 `#include "wb_harness.h"`，用 WB_CHECK("TC-xxx", 条件, 失败原因格式串, ...) 上报结果；
   用例编号必须与测试用例设计里的编号完全一致，每条设计用例至少一个 WB_CHECK。
3. main 固定形态：`int main(void) { wb_begin(); ... return wb_summary() == 0 ? 0 : 1; }`。
4. 测试代码自身同样受编码子集约束：不得用动态内存、递归、函数指针，缓冲区用定长数组。
5. 每个文件写成「## 文件 <相对路径>」标题 + ```c 围栏代码块；
   Markdown 最后追加一个 ```json 元数据块，源码不要写进 JSON。
"""

TEST_IMPL_SYSTEM = ("""你是一名资深嵌入式软件测试工程师，负责把文字测试用例翻译成可执行的 C 测试程序。
判据来自测试用例设计里的「预期结果」，不是来自被测实现：实现有缺陷时测试必须失败，
所以绝不允许为了让测试通过而迁就实现的实际行为。""" + TEST_IMPL_RULES)

TEST_IMPL_PROMPT = """请把下面的《测试用例设计》翻译成可执行的 C 测试程序。

## 编码子集（硬性判据）
{rules}

## 测试桩接口（系统已提供，直接包含使用，不要自行实现或改写）
```c
{harness}
```

## 被测模块 API（只给接口，不给实现：测试要按设计判据写，不能照着实现凑答案）
{api}

## 详细设计函数清单
{functions}

## 测试用例设计（必须逐条落地，编号不得改写、不得遗漏）
{cases}

## 实现要求
- 一条设计用例对应一个 WB_CHECK，编号原样使用（如 "TC-001"）。
- 失败原因格式串要能定位问题：把实际值打出来（如 `"crc=0x%04X 期望 0x29B1"`），
  不要只写 "failed"。格式串必须是单行文本。
- 每条用例前把被测上下文重新初始化，避免用例之间相互污染。
- 覆盖异常与边界用例时，注意不要写出必然越界的下标或未初始化读取——
  测试程序自己崩了，后面所有用例都会被记成「未执行」。

末尾 ```json 元数据格式：
{{"files": ["tests/test_comm.c"],
 "cases": [{{"id": "TC-001", "fn": "被测函数名", "fr_ids": ["FR-002"]}}]}}
（cases 必须覆盖上面每一条设计用例；fn 填该用例主要验证的函数名，
 fr_ids 沿用设计里的验证需求编号）
{retry_block}"""


ATTRIBUTION_SYSTEM = ("""你是一名资深嵌入式软件缺陷定位工程师。
一轮自动化验证未通过，你要判定责任方：被测代码的实现缺陷，还是测试代码的用例缺陷。
判定必须基于工具链给出的事实，不得凭猜测；拿不准时倾向于判为代码缺陷，
因为代码改动要重新过人工评审门，而测试改动同样要过门，两者代价相当，
但放过实现缺陷的代价更高。""" + COMMON_RULES)

ATTRIBUTION_PROMPT = """以下是本轮验证的工具链事实。

## 验证结论简报
{brief}

## 被测代码（src/ 与 include/）
{code}

## 测试代码（tests/）
{tests}

## 测试用例设计（判据来源）
{cases}

请判定责任方并说明依据。只输出一个 ```json 代码块：
{{"decision": "fix_code 或 fix_test",
 "reason": "一句话结论，要指出具体文件、函数或用例编号",
 "evidence": ["支撑该判定的事实，逐条列出，引用工具输出或代码行"],
 "hint": "给重生阶段的整改要点，逐条可执行"}}
（decision 只能是 fix_code 或 fix_test 二选一）"""


def _cell(value) -> str:
    """Markdown 表格单元格转义：竖线与换行会破坏表结构。"""
    return str(value if value is not None else "-").replace("|", "/").replace("\n", " ")


def _lld_functions(lld_meta) -> list[dict]:
    """详细设计的函数清单（名称已去空白），代码与测试实现两个阶段共用。"""
    out = []
    for f in (lld_meta or {}).get("functions") or []:
        if isinstance(f, dict) and str(f.get("name") or "").strip():
            out.append({"name": str(f["name"]).strip(),
                        "sig": str(f.get("sig") or "").strip(),
                        "desc": str(f.get("desc") or "").strip()})
    return out


def _functions_block(funcs: list[dict]) -> str:
    if not funcs:
        return "（详细设计未给出函数清单）"
    return "\n".join(f"- `{f['sig'] or f['name']}`　{f['desc']}" for f in funcs)


def _frs_block(srs_meta) -> str:
    frs = trace.collect_frs(srs_meta)
    if not frs:
        return "（上游未提供需求清单）"
    return "\n".join(f"- {f['id']}（{f['priority']}）{f['desc']}" for f in frs)


def _cases_block(tc_meta) -> str:
    """把测试用例设计压成表格喂给测试实现智能体。"""
    cases = [c for c in (tc_meta or {}).get("testcases") or [] if isinstance(c, dict)]
    if not cases:
        return "（无测试用例设计）"
    lines = ["| 编号 | 标题 | 类型 | 优先级 | 前置条件 | 步骤 | 预期结果 | 验证需求 |",
             "|---|---|---|---|---|---|---|---|"]
    for c in cases:
        steps = "；".join(str(s) for s in (c.get("steps") or []))
        lines.append(f"| {_cell(c.get('id'))} | {_cell(c.get('title'))} | "
                     f"{_cell(c.get('type'))} | {_cell(c.get('priority'))} | "
                     f"{_cell(c.get('preconditions'))} | {_cell(steps)} | "
                     f"{_cell(c.get('expected'))} | "
                     f"{_cell('、'.join(c.get('fr_ids') or []))} |")
    return "\n".join(lines)


def _expected_case_ids(tc_meta) -> list[str]:
    """测试用例设计里的全部编号（规范化后），测试实现阶段的硬校验基线。"""
    out = []
    for c in (tc_meta or {}).get("testcases") or []:
        if isinstance(c, dict) and c.get("id"):
            cid = parsers.norm_case_id(c["id"])
            if cid and cid not in out:
                out.append(cid)
    return out


_STR_LIT_RE = re.compile(r'"([^"\\\n]*)"')
_CASE_ID_RE = re.compile(r"^TC-\d+$")


def case_ids_in_source(text: str) -> set:
    """源码里作为字符串字面量出现的用例编号。

只看双引号字面量，注释里的 `/* TC-001 ... */` 不算数——
否则模型写满注释就能骗过「每条用例都落地」这条硬校验。"""
    out = set()
    for lit in _STR_LIT_RE.findall(text or ""):
        cid = parsers.norm_case_id(lit.strip())
        if _CASE_ID_RE.match(cid):
            out.add(cid)
    return out


def _probe_feedback(probe: dict | None, head: str) -> str:
    """把工具链结论转成回灌文本。工具输出原样带上：模型要靠它定位问题。"""
    if not probe or probe.get("ok"):
        return ""
    if probe.get("skipped"):
        return ""       # 未配置验证机时明确跳过，不能当成失败反复重试
    tail = "\n".join((probe.get("log") or "").strip().splitlines()[-40:])
    reason = probe.get("reason") or "工具链未通过"
    return f"{head}：{reason}\n工具链输出（尾部）：\n{tail or '（无输出）'}"


def generate_code(lld_markdown: str, lld_meta: dict, srs_meta: dict | None = None,
                  retry_comments: str | None = None,
                  previous_markdown: str | None = None,
                  compile_check=None, progress_cb=None) -> tuple[str, dict]:
    """代码实现：把详细设计落成受限 C 子集源码，硬校验 = 远端 gcc 编译通过。"""
    funcs = _lld_functions(lld_meta)
    names = [f["name"] for f in funcs]
    known_frs = {f["id"] for f in trace.collect_frs(srs_meta)}
    rendered = {"text": ""}

    def validate(meta):
        files = meta.get("files")
        if not isinstance(files, list) or not files:
            return "files 缺失或为空：必须列出本次输出的全部源文件相对路径"
        if any(not buildkit.normalize_path(str(p)) for p in files):
            return (f"files 中存在非法路径（只能是 src/*.c 或 include/*.h 的相对路径）："
                    f"{[p for p in files if not buildkit.normalize_path(str(p))]}")
        unknown = [k for k in (meta.get("derived_from") or {})
                   if str(k).strip() not in set(names)]
        if names and unknown:
            return f"derived_from 的键必须是详细设计函数清单里的函数名，未知函数：{unknown}"
        return None

    def doc_validate(markdown, meta):
        files = c_files.extract_files(markdown)
        if not files:
            return ("未能从正文抽出任何源文件：每个文件写成「## 文件 <相对路径>」标题，"
                    "紧跟一个 ```c 围栏代码块给出完整内容")
        fb = buildkit.layout_feedback(code_files=files)
        if fb:
            return "目录约定不符，请修正后重新输出完整代码：\n" + fb
        illegal = c_files.illegal_paths(files)
        if illegal:
            return f"以下文件不在约定目录（src/ 或 include/）下：{illegal}"
        rendered["text"] = "\n".join(str(v) for v in files.values())
        meta["files"] = c_files.file_list(files)
        if compile_check is not None:
            return _probe_feedback(compile_check(files), "远端编译未通过") or None
        return None

    def soft_validate(meta):
        # 设计里的函数一个都不能少：这是追溯矩阵「代码单元」列的前提
        if not names:
            return None
        text = rendered["text"]
        missing = [n for n in names
                   if not re.search(rf"(?<![\w]){re.escape(n)}\s*\(", text)]
        if missing:
            return f"详细设计要求实现、但代码中找不到定义的函数：{missing}"
        return None

    markdown, meta = run_agent(
        role="code",
        system_prompt=CODE_SYSTEM + RETRY_RULES,
        user_prompt=CODE_PROMPT.format(
            rules=c_rules.prompt_block(config.COMPLEXITY_MAX),
            lld=lld_markdown,
            functions=_functions_block(funcs),
            frs=_frs_block(srs_meta),
            retry_block=_retry_block(retry_comments, previous_markdown)),
        validator=validate,
        doc_validator=doc_validate,
        soft_validator=soft_validate,
        progress_cb=progress_cb,
    )
    files = c_files.extract_files(markdown)
    meta["files"] = c_files.file_list(files) or [str(p) for p in meta.get("files") or []]
    meta["derived_from"] = {
        str(k).strip(): trace.norm_refs(v, "fr")
        for k, v in (meta.get("derived_from") or {}).items()}
    if known_frs:
        # 编造的需求编号直接丢弃：宁可少一条追溯，也不要假追溯
        meta["derived_from"] = {k: [r for r in v if r in known_frs]
                                for k, v in meta["derived_from"].items()}
    meta["lld_functions"] = names
    return markdown, meta


def generate_test_impl(tc_meta: dict, code_files: dict | None = None,
                       lld_meta: dict | None = None, srs_meta: dict | None = None,
                       retry_comments: str | None = None,
                       previous_markdown: str | None = None,
                       compile_check=None, progress_cb=None) -> tuple[str, dict]:
    """测试实现：把文字用例翻译成可执行 C 测试，硬校验 = 远端编译 + 链接通过。

    只给头文件（API），不给 src/ 实现。这是 V 模型的关键约束：
    测试一旦照着实现写，实现里的缺陷就会被测试原样接受，验证等于自证。"""
    expected = _expected_case_ids(tc_meta)
    frs = trace.collect_frs(srs_meta)
    known_frs = {f["id"] for f in frs}
    case_fr = {}
    for c in (tc_meta or {}).get("testcases") or []:
        if isinstance(c, dict) and c.get("id"):
            case_fr[parsers.norm_case_id(c["id"])] = trace.norm_refs(c.get("fr_ids"), "fr")
    headers = {p: c for p, c in (code_files or {}).items()
               if buildkit.normalize_path(p).startswith("include/")}
    api = (c_files.render_files(headers, title="被测模块 API")
           if headers else "（被测模块没有单独的头文件，请按详细设计的函数签名调用）")
    rendered = {"text": ""}

    def validate(meta):
        files = meta.get("files")
        if not isinstance(files, list) or not files:
            return "files 缺失或为空"
        bad = [p for p in files
               if not buildkit.normalize_path(str(p)).startswith("tests/")]
        if bad:
            return f"测试代码只能放在 tests/ 下，以下路径不合法：{bad}"
        cases = meta.get("cases")
        if not isinstance(cases, list) or not cases:
            return "cases 缺失或为空：必须逐条登记落地的用例"
        got = {parsers.norm_case_id(c.get("id")) for c in cases if isinstance(c, dict)}
        miss = [cid for cid in expected if cid not in got]
        if miss:
            return (f"以下测试用例设计里的用例没有落地：{miss}。"
                    "每条设计用例都必须有一个对应的 WB_CHECK，编号原样使用")
        return None

    def doc_validate(markdown, meta):
        files = c_files.extract_files(markdown)
        if not files:
            return ("未能从正文抽出任何源文件：每个文件写成「## 文件 <相对路径>」标题，"
                    "紧跟一个 ```c 围栏代码块")
        fb = buildkit.layout_feedback(test_files=files)
        if fb:
            return "目录约定不符，请修正后重新输出完整测试代码：\n" + fb
        text = "\n".join(str(v) for v in files.values())
        rendered["text"] = text
        meta["files"] = c_files.file_list(files)
        # 元数据里登记了不等于代码里真写了：以源码里的字符串字面量为准
        in_src = case_ids_in_source(text)
        miss = [cid for cid in expected if cid not in in_src]
        if miss:
            return (f"源码中没有为以下用例写出 WB_CHECK（编号必须作为字符串字面量出现）："
                    f"{miss}")
        if compile_check is not None:
            return _probe_feedback(compile_check(files), "远端编译或链接未通过") or None
        return None

    def soft_validate(meta):
        cases = [c for c in meta.get("cases") or [] if isinstance(c, dict)]
        orphan = [str(c.get("id")) for c in cases
                  if not trace.norm_refs(c.get("fr_ids"), "fr")]
        if orphan:
            return f"以下用例没有标注验证的需求编号 fr_ids：{orphan}"
        nofn = [str(c.get("id")) for c in cases if not str(c.get("fn") or "").strip()]
        if nofn:
            return f"以下用例没有标注被测函数名 fn：{nofn}"
        return None

    markdown, meta = run_agent(
        role="test_impl",
        system_prompt=TEST_IMPL_SYSTEM + RETRY_RULES,
        user_prompt=TEST_IMPL_PROMPT.format(
            rules=c_rules.prompt_block(config.COMPLEXITY_MAX),
            harness=buildkit.HARNESS_H.strip(),
            api=api,
            functions=_functions_block(_lld_functions(lld_meta)),
            cases=_cases_block(tc_meta),
            retry_block=_retry_block(retry_comments, previous_markdown)),
        validator=validate,
        doc_validator=doc_validate,
        soft_validator=soft_validate,
        progress_cb=progress_cb,
    )
    files = c_files.extract_files(markdown)
    meta["files"] = c_files.file_list(files) or [str(p) for p in meta.get("files") or []]
    norm = []
    for c in meta.get("cases") or []:
        if not isinstance(c, dict):
            continue
        cid = parsers.norm_case_id(c.get("id"))
        refs = trace.norm_refs(c.get("fr_ids"), "fr")
        if known_frs:
            refs = [r for r in refs if r in known_frs]
        if not refs:
            refs = case_fr.get(cid, [])      # 模型漏标时回填设计里的编号
        norm.append({"id": cid, "fn": str(c.get("fn") or "").strip(),
                     "fr_ids": refs})
    meta["cases"] = norm
    meta["case_fr"] = {c["id"]: c["fr_ids"] for c in norm}
    meta["expected_ids"] = expected
    return markdown, meta


def attribute_failure(brief: str, code_files: dict | None = None,
                      test_files: dict | None = None, tc_meta: dict | None = None,
                      progress_cb=None) -> tuple[str, dict]:
    """失败归因：判定这一轮未通过的责任方是代码还是测试。

    只在确定性判据（executor.auto_decision）给不出结论时才调用：
    构建失败的诊断落点、用例全过而覆盖不足这类情形工具链已经判死，不必问模型。"""
    def validate(meta):
        if meta.get("decision") not in ("fix_code", "fix_test"):
            return "decision 必须是 fix_code 或 fix_test"
        if not str(meta.get("reason") or "").strip():
            return "reason 不能为空"
        return None

    code_text = c_files.render_files(code_files or {}, title="被测代码") if code_files else "（无）"
    test_text = c_files.render_files(test_files or {}, title="测试代码") if test_files else "（无）"
    _md, meta = run_agent(
        role="attribution",
        system_prompt=ATTRIBUTION_SYSTEM,
        user_prompt=ATTRIBUTION_PROMPT.format(
            brief=brief, code=code_text[:24000], tests=test_text[:24000],
            cases=_cases_block(tc_meta)),
        validator=validate,
        expect_json_only=True,
        progress_cb=progress_cb,
    )
    label = "被测代码缺陷" if meta["decision"] == "fix_code" else "测试代码缺陷"
    lines = ["# 失败归因结论", "",
             f"> 判定：**{label}**（decision = `{meta['decision']}`）", "",
             f"## 结论\n\n{meta.get('reason', '')}", ""]
    ev = [str(x) for x in (meta.get("evidence") or []) if str(x).strip()]
    if ev:
        lines += ["## 判定依据", ""] + [f"- {x}" for x in ev] + [""]
    hints = [str(x) for x in (meta.get("hint") or []) if str(x).strip()]
    if isinstance(meta.get("hint"), str) and meta["hint"].strip():
        hints = [meta["hint"].strip()]
    if hints:
        lines += ["## 整改要点", ""] + [f"- {x}" for x in hints] + [""]
    lines += ["## 工具链事实（归因输入）", "", "```text", brief.strip(), "```"]
    meta["evidence"] = ev
    meta["hint"] = hints
    return "\n".join(lines), meta
