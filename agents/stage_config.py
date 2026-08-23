"""智能体配置和执行框架：消除代码重复。

主要改进：
1. 用配置驱动替代重复的函数定义
2. 统一的验证逻辑
3. 可扩展的阶段定义
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from agents.base_agent import run_agent


@dataclass
class FieldValidator:
    """字段验证规则"""
    field_path: str  # JSON路径，如 "testcases[*].id"
    required: bool = True
    field_type: Optional[type] = None
    allowed_values: Optional[List[str]] = None


@dataclass
class StageConfig:
    """阶段配置：描述一个智能体阶段的所有信息"""
    stage_name: str  # requirement / hld / lld / testcase
    role: str  # LLM角色名
    system_prompt: str
    user_prompt_template: str  # 包含 {input} 和 {retry_block} 占位符

    # 输入准备函数：将上游产物转为 prompt 输入
    prepare_input: Callable[[dict], str]

    # 元数据验证规则
    validators: List[FieldValidator] = field(default_factory=list)

    # 输出处理
    expect_json_only: bool = False  # 是否只输出JSON（如测试用例）
    render_markdown: Optional[Callable[[dict], str]] = None  # JSON→Markdown渲染函数

    # 文档标题
    title: str = ""


def _build_validator(validators: List[FieldValidator]) -> Callable[[dict], Optional[str]]:
    """根据验证规则列表构建验证函数"""
    def validate(meta: dict) -> Optional[str]:
        for v in validators:
            # 简单的路径解析（支持 "field" 和 "array[*].field"）
            if "[*]" in v.field_path:
                # 数组字段：如 "testcases[*].id"
                parts = v.field_path.split("[*].")
                array_key = parts[0]
                item_key = parts[1] if len(parts) > 1 else None

                array = meta.get(array_key)
                if v.required and (not isinstance(array, list) or not array):
                    return f"{array_key} 缺失或为空"

                if item_key and isinstance(array, list):
                    for idx, item in enumerate(array):
                        if v.required and item_key not in item:
                            return f"{array_key}[{idx}] 缺少字段 {item_key}"
                        if v.field_type and item_key in item:
                            if not isinstance(item[item_key], v.field_type):
                                return f"{array_key}[{idx}].{item_key} 类型错误"
                        if v.allowed_values and item_key in item:
                            if item[item_key] not in v.allowed_values:
                                return f"{array_key}[{idx}].{item_key} 值非法: {item[item_key]}"
            else:
                # 简单字段：如 "modules"
                value = meta.get(v.field_path)
                if v.required and not value:
                    return f"{v.field_path} 缺失或为空"
                if v.field_type and value is not None:
                    if not isinstance(value, v.field_type):
                        return f"{v.field_path} 类型错误，期望 {v.field_type.__name__}"

        return None

    return validate


def execute_stage(
    config: StageConfig,
    input_data: dict,
    retry_comments: Optional[str] = None,
    previous_markdown: Optional[str] = None,
    progress_cb: Optional[Callable] = None
) -> tuple[str, dict]:
    """执行一个阶段（通用逻辑）

    Args:
        config: 阶段配置
        input_data: 输入数据（上游产物）
        retry_comments: 评审意见（驳回时）
        previous_markdown: 上一版产物（驳回时）
        progress_cb: 进度回调

    Returns:
        (markdown: str, meta: dict)
    """
    # 准备输入
    input_str = config.prepare_input(input_data)

    # 构建重试块
    retry_block = ""
    if retry_comments:
        retry_block = f"""
---
【修订任务】
评审意见（必须逐条回应）：
{retry_comments}

上一版产物（请在此基础上修订）：
{previous_markdown or "（无）"}
"""

    # 构建用户提示
    user_prompt = config.user_prompt_template.format(
        input=input_str,
        retry_block=retry_block
    )

    # 构建验证器
    validator = _build_validator(config.validators)

    # 执行智能体
    markdown, meta = run_agent(
        role=config.role,
        system_prompt=config.system_prompt,
        user_prompt=user_prompt,
        validator=validator,
        expect_json_only=config.expect_json_only,
        progress_cb=progress_cb
    )

    # 如果是JSON输出，需要渲染Markdown
    if config.expect_json_only and config.render_markdown:
        markdown = config.render_markdown(meta)

    return markdown, meta


# ==================== 阶段配置定义 ====================

COMMON_RULES = """
输出约定（必须严格遵守）：
1. 全部文档内容使用 Markdown 格式；所有图表必须使用 Mermaid 语法（```mermaid 代码块）。
2. 在 Markdown 文档的最后，追加一个 ```json 代码块作为元数据，供系统校验与下游引用。
3. 内容必须基于给定的输入材料，不得编造输入中不存在的核心需求；对材料未覆盖之处应明确指出。
4. 默认目标语言为 C 语言（嵌入式/裸机风格）：使用 struct、typedef、函数指针、模块划分等 C 范式，
   禁止使用类、对象、继承、多态等面向对象概念；图表中不要出现 classDiagram。
5. 如果是修订任务：输入中会给出上一版产物与评审意见，你必须逐条回应评审意见，
   并在文档开头添加「修订说明」章节，逐条列出意见及对应修改。
"""


# 需求分析阶段
REQUIREMENT_CONFIG = StageConfig(
    stage_name="requirement",
    role="requirement",
    title="软件需求规格说明书",
    system_prompt=f"""你是一名资深需求分析师，擅长将原始需求材料整理为规范的软件需求规格说明书（SRS）。{COMMON_RULES}""",
    user_prompt_template="""请基于以下《结构化原始数据》撰写《软件需求规格说明书》。

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
{{input}}
{{retry_block}}""",
    prepare_input=lambda data: data.get("structured_json", ""),
    validators=[
        FieldValidator("functional_requirements", required=True, field_type=list),
        FieldValidator("functional_requirements[*].id", required=True),
        FieldValidator("functional_requirements[*].desc", required=True),
        FieldValidator("functional_requirements[*].priority", required=True,
                      allowed_values=["P0", "P1", "P2"]),
    ]
)


# 概要设计阶段
HLD_CONFIG = StageConfig(
    stage_name="hld",
    role="hld",
    title="概要设计说明书",
    system_prompt=f"""你是一名资深系统架构师，擅长基于需求规格说明书产出概要设计。{COMMON_RULES}""",
    user_prompt_template="""请基于以下《软件需求规格说明书》撰写《概要设计说明书》。

要求包含章节：
1. 系统模块划分：表格（模块、职责、依赖）
2. 技术架构：说明技术选型，并给出 Mermaid `graph TD` 架构图
3. 核心业务流程：至少一个 Mermaid `flowchart` 流程图
4. 数据库设计：核心表 DDL（```sql），表间关系说明
5. 核心 API：表格（方法、路径、说明）

末尾 ```json 元数据格式：
{{"modules": ["..."], "tables": ["..."], "apis": [{{"method","path","desc"}}]}}

输入材料：
{{input}}
{{retry_block}}""",
    prepare_input=lambda data: f"""《软件需求规格说明书》全文：
{data.get('srs_markdown', '')}

需求元数据：
{data.get('srs_meta_json', '')}""",
    validators=[
        FieldValidator("modules", required=True, field_type=list),
        FieldValidator("tables", required=True, field_type=list),
        FieldValidator("apis", required=True, field_type=list),
    ]
)


# 详细设计阶段
LLD_CONFIG = StageConfig(
    stage_name="lld",
    role="lld",
    title="详细设计说明书",
    system_prompt=f"""你是一名资深软件设计师，擅长基于概要设计逐模块细化详细设计。{COMMON_RULES}""",
    user_prompt_template="""请基于以下《概要设计说明书》（及其需求背景）撰写《详细设计说明书》。
目标语言为 C（无类概念，禁止面向对象设计）。

要求包含章节：
1. 各核心模块数据结构设计：Mermaid `flowchart LR` 模块结构图（节点为模块/struct），
   并用 ```c 代码块给出关键 struct / typedef / 枚举定义及字段注释
2. 接口定义：逐函数给出 C 函数签名、参数说明、返回值、异常情况
3. 核心业务序列图：至少一个 Mermaid `sequenceDiagram`（参与者为模块/函数，消息为函数调用）
4. 关键业务流程细化说明（含状态流转、内存/资源管理等 C 相关要点）

末尾 ```json 元数据格式：
{{"data_structures": ["..."], "functions": [{{"name","sig","desc"}}]}}

输入材料：
{{input}}
{{retry_block}}""",
    prepare_input=lambda data: f"""《概要设计说明书》全文：
{data.get('hld_markdown', '')}

概要设计元数据：
{data.get('hld_meta_json', '')}

需求背景（SRS 摘录）：
{data.get('srs_markdown', '')[:3000]}""",
    validators=[
        FieldValidator("data_structures", required=True, field_type=list),
        FieldValidator("functions", required=True, field_type=list),
    ]
)


# 测试用例阶段
def _render_testcase_markdown(meta: dict) -> str:
    """将测试用例JSON渲染为Markdown"""
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
    return "\n".join(lines)


TESTCASE_CONFIG = StageConfig(
    stage_name="testcase",
    role="testcase",
    title="测试用例设计",
    system_prompt=f"""你是一名资深测试工程师，擅长根据需求与设计文档设计全面的结构化测试用例。{COMMON_RULES}
5. 最终只输出一个 ```json 代码块（不要输出 Markdown 文档正文），系统会自动渲染。""",
    user_prompt_template="""请根据以下需求与设计材料生成结构化测试用例。

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

输入材料：
{{input}}
{{retry_block}}""",
    prepare_input=lambda data: f"""《软件需求规格说明书》全文：
{data.get('srs_markdown', '')}

需求元数据：
{data.get('srs_meta_json', '')}""",
    validators=[
        FieldValidator("testcases", required=True, field_type=list),
        FieldValidator("testcases[*].id", required=True),
        FieldValidator("testcases[*].preconditions", required=True),
        FieldValidator("testcases[*].steps", required=True, field_type=list),
        FieldValidator("testcases[*].expected", required=True),
        FieldValidator("testcases[*].type", allowed_values=["功能", "边界", "异常", "场景"]),
        FieldValidator("testcases[*].priority", allowed_values=["P0", "P1", "P2"]),
    ],
    expect_json_only=True,
    render_markdown=_render_testcase_markdown
)


# 配置注册表
STAGE_CONFIGS = {
    "requirement": REQUIREMENT_CONFIG,
    "hld": HLD_CONFIG,
    "lld": LLD_CONFIG,
    "testcase": TESTCASE_CONFIG,
}


def get_stage_config(stage_name: str) -> StageConfig:
    """获取阶段配置"""
    if stage_name not in STAGE_CONFIGS:
        raise ValueError(f"未知阶段: {stage_name}")
    return STAGE_CONFIGS[stage_name]
