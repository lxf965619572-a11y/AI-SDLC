"""航天嵌入式 C 受限子集规则表：生成器提示词与静态检查的单一事实来源。

第一性原理：AI 写的代码要能进交付件，判据就必须是机器可复核的，而不是「看着像对的」。
于是规则先落成这张表，再由表导出两份东西——
  1. 代码生成智能体的提示词约束（prompt_block）；
  2. 静态检查器的判定项（core.c_static 按 rule.id 出结论）。
同一张表导出，就不会出现「提示词说不许 malloc、检查器却不管」这种漂移。

standard_ref 只写标准名，不写条款号：条款号由标准化部门核对后填入，
开发阶段不阻塞；rule.id 与判定行为本期冻结，改判据要走评审。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 必查项：不通过就不能放行，只能整改或走偏差单 + 人工批准
SEV_REQUIRED = "required"
# 建议项：出结论、进报告，但不阻断流水线
SEV_ADVISORY = "advisory"

SEVERITY_TEXT = {SEV_REQUIRED: "必查", SEV_ADVISORY: "建议"}

# 动态内存接口名单（WB-C-001）。含常见别名，避免换个名字就绕过判据。
DYNAMIC_ALLOC_FUNCS = frozenset({
    "malloc", "calloc", "realloc", "free", "strdup", "strndup", "alloca",
    "__builtin_alloca", "aligned_alloc", "valloc", "pvalloc", "memalign",
    "posix_memalign", "reallocarray", "cfree", "operator new", "operator delete",
})


@dataclass(frozen=True)
class Rule:
    """一条编码规则。

    waivable=False 表示「不可偏差」：航天嵌入式里动态内存与递归属于安全性底线，
    即便人工也不能批准放行，只能改代码。
    """
    id: str
    title: str
    standard_ref: str
    severity: str
    waivable: bool
    why: str
    prompt: str
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocking(self) -> bool:
        """是否阻断流水线（必查项阻断，建议项只记录）。"""
        return self.severity == SEV_REQUIRED


_GJB = "GJB 8114《C/C++语言编程安全子集》（条款号待标准化部门核定）"
_QJ = "QJ 20084《航天嵌入式软件编码规范》（条款号待标准化部门核定）"

RULES: list[Rule] = [
    Rule(
        id="WB-C-000",
        title="源码必须落在受限子集内且可被静态解析",
        standard_ref=_GJB,
        severity=SEV_REQUIRED, waivable=False,
        why="静态检查基于 C99 AST。解析不了就没有结论，而没有结论不能当作合格——"
            "条件编译与函数式宏会让同一份源码在不同配置下语义不同，交付件不可复现。",
        prompt="不得使用条件编译（#if / #ifdef / #ifndef / #else / #elif / #endif）、"
               "函数式宏（带参数的 #define）、#pragma，以及 C99 之外的语法；"
               "只允许对象式 #define（无参数）与 #include。",
        tags=("parse",),
    ),
    Rule(
        id="WB-C-001",
        title="禁止动态内存分配",
        standard_ref=_GJB,
        severity=SEV_REQUIRED, waivable=False,
        why="堆分配在长周期运行的星载软件里会引入碎片与不确定时延，"
            "失败路径还难以穷尽测试。所有缓冲区必须编译期确定。",
        prompt="禁止调用 malloc / calloc / realloc / free / strdup / alloca 等任何动态内存接口；"
               "缓冲区一律用定长数组，或由调用方通过上下文结构体指针传入。",
        tags=("memory",),
    ),
    Rule(
        id="WB-C-002",
        title="禁止递归",
        standard_ref=_GJB,
        severity=SEV_REQUIRED, waivable=False,
        why="递归的栈占用取决于运行期数据，无法在集成前给出最坏栈深度，"
            "等于把栈溢出风险带进在轨运行阶段。",
        prompt="函数不得直接或间接调用自身；需要重复处理时用有界循环，"
               "需要下探时用显式的定长栈数组并检查栈满。",
        tags=("stack",),
    ),
    Rule(
        id="WB-C-003",
        title="禁止函数指针",
        standard_ref=_GJB,
        severity=SEV_REQUIRED, waivable=True,
        why="函数指针让调用关系在编译期不可见，静态分析与覆盖率都无法闭合，"
            "被篡改后还是典型的控制流劫持入口。",
        prompt="不得声明或使用函数指针，包括回调参数、函数指针数组、"
               "结构体中的函数指针成员；需要分派时用枚举 + switch。",
        tags=("control_flow",),
    ),
    Rule(
        id="WB-C-004",
        title="数组必须静态定长",
        standard_ref=_GJB,
        severity=SEV_REQUIRED, waivable=True,
        why="变长数组把尺寸决定推迟到运行期，栈需求随之不可静态确定，"
            "与禁止动态内存是同一条安全底线的两个侧面。",
        prompt="禁止变长数组（VLA）。数组维度必须是编译期常量："
               "用 enum { NAME = N }; 或对象式 #define，不得用函数参数或局部变量做维度。"
               "函数形参可以写成 buf[] （等价于指针），但必须另有长度参数。",
        tags=("memory",),
    ),
    Rule(
        id="WB-C-005",
        title="函数单出口",
        standard_ref=_QJ,
        severity=SEV_ADVISORY, waivable=True,
        why="单出口让资源释放与状态恢复只有一处，降低漏放概率；"
            "但过度追求会催生深层嵌套，故列为建议项。",
        prompt="每个函数尽量只保留一个 return 语句，放在函数末尾；"
               "提前返回用结果变量 + 末尾统一 return 表达。",
        tags=("structure",),
    ),
    Rule(
        id="WB-C-006",
        title="圈复杂度不超过上限",
        standard_ref=_QJ,
        severity=SEV_REQUIRED, waivable=True,
        why="复杂度直接决定独立路径条数，也就是决定这条需求要写多少用例才叫测完。"
            "上限可调，但不能没有上限。",
        prompt="单函数圈复杂度不超过 {complexity_max}"
               "（判定点 = if / for / while / do / case / && / || / ?:）；"
               "超出就拆函数，不要用 goto 或多层嵌套硬塞。",
        tags=("structure",),
    ),
    Rule(
        id="WB-C-007",
        title="禁止可写的静态存储变量",
        standard_ref=_GJB,
        severity=SEV_REQUIRED, waivable=True,
        why="隐式全局状态让函数不再自洽：同样的入参可能给出不同结果，"
            "单元测试无法独立复现，多任务环境下还会引入未受控的共享。"
            "把状态收进显式传入的上下文结构体，函数才是可验证的纯逻辑。",
        prompt="不得声明非 const 的文件作用域变量，也不得在函数内声明 static 变量；"
               "模块状态一律收敛到显式传入的上下文结构体指针（如 Ctx *ctx）。"
               "只读常量表用 static const 声明。",
        tags=("state",),
    ),
    Rule(
        id="WB-D-001",
        title="详细设计的每个函数都必须在代码中实现",
        standard_ref=_QJ,
        severity=SEV_REQUIRED, waivable=False,
        why="这是追溯矩阵「代码单元」列的判据。设计里有、代码里没有，"
            "意味着需求链在实现环节断了，而断链在评审时最容易被漏掉。",
        prompt="必须实现详细设计 functions 清单中的每一个函数，函数名与设计完全一致。",
        tags=("design",),
    ),
    Rule(
        id="WB-D-002",
        title="实现签名必须与详细设计一致",
        standard_ref=_QJ,
        severity=SEV_REQUIRED, waivable=True,
        why="签名漂移会让设计文档与实际接口对不上，"
            "后续按文档做的测试与集成都会建立在错误前提上。",
        prompt="函数返回类型与参数类型、顺序必须与详细设计给出的签名一致；"
               "确需调整时同步更新详细设计并说明理由。",
        tags=("design",),
    ),
    Rule(
        id="WB-D-003",
        title="代码中不得出现设计外的公开函数",
        standard_ref=_QJ,
        severity=SEV_ADVISORY, waivable=True,
        why="设计外的公开接口没有需求来源，属于未经评审的攻击面与测试盲区。"
            "内部辅助函数应当声明为 static，不计入本项。",
        prompt="只对详细设计中列出的函数提供外部链接；"
               "内部辅助函数一律加 static，不要暴露设计外的公开接口。",
        tags=("design",),
    ),
]

_BY_ID: dict[str, Rule] = {r.id: r for r in RULES}


def get(rule_id: str) -> Rule | None:
    return _BY_ID.get(rule_id)


def title_of(rule_id: str) -> str:
    r = _BY_ID.get(rule_id)
    return r.title if r else rule_id


def blocking(rule_id: str) -> bool:
    """该规则是否阻断（必查项阻断）。未知 id 保守按阻断处理。"""
    r = _BY_ID.get(rule_id)
    return True if r is None else r.blocking


def waivable(rule_id: str) -> bool:
    r = _BY_ID.get(rule_id)
    return False if r is None else r.waivable


def severity_of(rule_id: str) -> str:
    r = _BY_ID.get(rule_id)
    return SEV_REQUIRED if r is None else r.severity


def code_rules() -> list[Rule]:
    """编码子集规则（WB-C-*），按 id 排序，用于提示词。"""
    return sorted((r for r in RULES if r.id.startswith("WB-C-")), key=lambda r: r.id)


def design_rules() -> list[Rule]:
    """设计一致性规则（WB-D-*）。"""
    return sorted((r for r in RULES if r.id.startswith("WB-D-")), key=lambda r: r.id)


def render(text: str, complexity_max: int = 10) -> str:
    """把规则文本里的 `{complexity_max}` 占位符替换成实际上限。

    不用 str.format：规则判据是写给 C 工程师看的散文，里面本来就会出现
    `enum { NAME = N }` 这类花括号字面量，format 会把它当字段名解析并抛 KeyError。
    提示词与静态检查报告都走这里，避免两边各写一套替换逻辑。"""
    return str(text or "").replace("{complexity_max}", str(complexity_max))


def prompt_block(complexity_max: int = 10) -> str:
    """导出给代码生成智能体的约束清单。

    与静态检查同源：这里写了什么，检查器就会抓什么，
    模型不会因为「提示词没说」而写出必然违规的代码。
    """
    lines = []
    for r in code_rules():
        if r.id == "WB-C-000":
            continue    # 解析类约束单独在提示词里说清楚，避免与子集说明重复
        lines.append(f"- {r.id} {r.title}：{render(r.prompt, complexity_max)}")
    subset = _BY_ID["WB-C-000"]
    lines.insert(0, f"- {subset.id} {subset.title}：{subset.prompt}")
    return "\n".join(lines)


def rule_table_markdown() -> str:
    """规则表本身也是交付件的一部分（评审要看判据来自哪里）。"""
    lines = ["| 规则编号 | 判据 | 等级 | 可偏差 | 标准依据 |",
             "|---|---|---|---|---|"]
    for r in RULES:
        lines.append(f"| {r.id} | {r.title} | {SEVERITY_TEXT[r.severity]} | "
                     f"{'是' if r.waivable else '否'} | {r.standard_ref} |")
    return "\n".join(lines)


def as_dicts() -> list[dict]:
    return [{"id": r.id, "title": r.title, "standard_ref": r.standard_ref,
             "severity": r.severity, "waivable": r.waivable, "why": r.why}
            for r in RULES]
