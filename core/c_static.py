"""受限 C 子集静态检查：pycparser AST 判定 + 设计覆盖检查。

第一性原理：静态检查的结论要能当判据用，就必须可复核——每条违规都给出
规则编号（core.c_rules 里的同一条）、文件、行号、函数与理由，人能照着源码核对。

为什么用 pycparser 而不是 clang-tidy / cppcheck：
  1. 判据要与生成器提示词同源（同一张规则表），自己实现访问器最直接；
  2. 纯 Python、零外部可执行文件依赖，Windows 编排侧不用装工具链；
  3. 解析不了的情况本身就是结论（WB-C-000），不会静默放行。
远端 gcc 编译是兜底判据：pycparser 说没问题但 gcc 报错，一样算失败。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pycparser import c_ast, c_parser

from core import c_rules

# ---- 预置类型：pycparser 没有预处理器，<stdint.h> 之类被剥掉后需要手工补 ----
# 这段前导会让行号整体偏移，所有对外报告的行号都要减掉 PRELUDE_LINES。
PRELUDE = """typedef signed char int8_t;
typedef unsigned char uint8_t;
typedef short int16_t;
typedef unsigned short uint16_t;
typedef int int32_t;
typedef unsigned int uint32_t;
typedef long int64_t;
typedef unsigned long uint64_t;
typedef long intptr_t;
typedef unsigned long uintptr_t;
typedef unsigned long size_t;
typedef long ssize_t;
typedef long ptrdiff_t;
typedef int wchar_t;
typedef int bool;
"""
PRELUDE_LINES = PRELUDE.count("\n")

# 标准头里最常用的宏。作为宏表参与展开，而不是写进 PRELUDE——
# 预处理发生在拼接 PRELUDE 之前，写进去也不会被展开。
PRELUDE_MACROS = {"NULL": "((void*)0)", "true": "1", "false": "0",
                  "TRUE": "1", "FALSE": "0", "EOF": "(-1)"}

_OBJ_DEFINE = re.compile(r"^#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.*)$")
_FUNC_DEFINE = re.compile(r"^#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# 标识符只认 ASCII：\w 会吃进中文，注释里的汉字被当成标识符的一部分，
# 宏替换就会把注释文本改坏，进而让 pycparser 报错（假的 WB-C-000）。
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TYPE_WORDS = {"unsigned", "signed", "long", "short", "int", "char", "void",
               "float", "double", "bool", "_Bool"}

# 预处理阶段就判定为「子集外」的指令（WB-C-000）
_COND_DIRECTIVES = ("if", "ifdef", "ifndef", "elif", "else", "endif")


# ============================ 违规记录 ============================
@dataclass
class Violation:
    """一条静态检查结论。行号已折算回原始源码行。"""
    rule_id: str
    file: str
    line: int
    message: str
    func: str = ""
    severity: str = ""
    title: str = ""
    waivable: bool = False
    standard_ref: str = ""

    def __post_init__(self):
        rule = c_rules.get(self.rule_id)
        if rule is not None:
            self.severity = self.severity or rule.severity
            self.title = self.title or rule.title
            self.waivable = rule.waivable
            self.standard_ref = self.standard_ref or rule.standard_ref
        else:
            self.severity = self.severity or c_rules.SEV_REQUIRED

    @property
    def blocking(self) -> bool:
        return self.severity == c_rules.SEV_REQUIRED

    def to_dict(self) -> dict:
        return {"rule_id": self.rule_id, "title": self.title, "file": self.file,
                "line": self.line, "func": self.func, "message": self.message,
                "severity": self.severity, "waivable": self.waivable,
                "standard_ref": self.standard_ref}

    def sort_key(self):
        return (0 if self.blocking else 1, self.rule_id, self.file, self.line)


@dataclass
class FileFacts:
    """单个源文件的解析事实，供检查器与报告共用。"""
    path: str
    lines: int = 0
    includes: list = field(default_factory=list)
    macros: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)       # 子集外构造（WB-C-000）
    parse_error: str = ""
    ast: object = None
    # TU = PRELUDE + 头文件前导 + 本文件；坐标要减掉这个偏移才回到本文件行号
    line_offset: int = PRELUDE_LINES


# ============================ 迷你预处理器 ============================
def _strip_comments(text: str, in_block_comment: bool = False) -> tuple[str, bool]:
    """删掉一行里的注释。pycparser 不认注释（会直接报 Comments are not supported），
    真实工具链里这一步由 cpp 完成，我们没有 cpp，只能自己剥。

    块注释换成一个空格（防 `a/*x*/b` 被粘成 `ab`），行注释丢到行尾。
    跨行块注释的状态由第二个返回值带出，供 preprocess 逐行串联——否则注释里的
    撇号、引号会被当成字面量起始，把后面整段代码吞掉。
    字符串与字符字面量里的 // 与 /* 不是注释，原样保留。"""
    in_comment = in_block_comment
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_comment:
            end = text.find("*/", i)
            if end < 0:
                i = n                          # 整行余下都在块注释里
            else:
                out.append(" ")                # 注释收口处补一个空格做分隔
                i = end + 2
                in_comment = False
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            in_comment = True
            i += 2
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i = n                              # 行注释：丢到行尾
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                out.append(text[i])
                done = text[i] == quote
                i += 1
                if done:
                    break
            continue
        out.append(ch)
        i += 1
    return "".join(out), in_comment


def _expand(text: str, macros: dict, in_block_comment: bool = False) -> tuple[str, bool]:
    """先剥注释，再按标识符展开对象式宏（跳过字符串/字符字面量）。

    返回 (处理结果, 行尾是否仍处于块注释中)。
    宏值里可能又引用别的宏（#define A B / #define B 4），所以迭代到不再变化，
    并用次数上限兜住自引用宏（#define A A）造成的死循环。"""
    text, in_comment = _strip_comments(text, in_block_comment)
    for _ in range(8):
        out: list[str] = []
        i, n = 0, len(text)
        changed = False
        while i < n:
            ch = text[i]
            if ch in "\"'":
                quote = ch
                out.append(ch)
                i += 1
                while i < n:
                    if text[i] == "\\" and i + 1 < n:
                        out.append(text[i:i + 2])
                        i += 2
                        continue
                    out.append(text[i])
                    done = text[i] == quote
                    i += 1
                    if done:
                        break
                continue
            m = _IDENT.match(text, i)
            if m:
                word = m.group(0)
                i = m.end()
                if word in macros:
                    out.append(macros[word])
                    changed = True
                else:
                    out.append(word)
                continue
            out.append(ch)
            i += 1
        text = "".join(out)
        if not changed:
            break
    return text, in_comment


def preprocess(source: str, seed_macros: dict | None = None) -> tuple[str, list, dict, list]:
    """把源码整理成 pycparser 能吃的形式，且严格保持行号不变。

    保持行号是硬要求：违规结论要能指回原始文件的某一行，
    删行会让后面所有行号错位，评审时对不上就没法核对。
    做法是把指令行原地替换成等量空行，而不是删除。

    seed_macros：来自项目头文件的宏表。源码里 #include 的头被剥成空行后，
    头里的 #define 就不在作用域里了；先把它们灌进来，源码用到的 COMM_WINDOW
    之类才能展开成常量，数组定长判定（WB-C-004）才不会误报。

    返回 (处理后文本, includes, macros, notes)。notes 是子集外构造的说明。
    """
    macros = dict(PRELUDE_MACROS)
    if seed_macros:
        macros.update(seed_macros)
    includes: list[str] = []
    notes: list[tuple[int, str]] = []
    raw_lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    in_bc = False            # 是否仍处于跨行块注释中（注释里的 # 不是指令）
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        lineno = i + 1
        stripped = line.strip()
        if stripped.startswith("#") and not in_bc:
            # 处理反斜杠续行：整条指令可能跨多行，全部替换成空行以保持行号
            j = i
            buf = [line.rstrip()]
            while buf[-1].endswith("\\") and j + 1 < len(raw_lines):
                j += 1
                buf.append(raw_lines[j].rstrip())
            directive = " ".join(b[:-1] if b.endswith("\\") else b for b in buf).strip()
            kind = re.sub(r"^#\s*", "", directive).split(None, 1)[0].lower()
            if kind == "include":
                m = re.search(r"<([^>]+)>|\"([^\"]+)\"", directive)
                includes.append(m.group(1) or m.group(2) if m else directive)
            elif kind == "define":
                if _FUNC_DEFINE.match(directive):
                    name = _FUNC_DEFINE.match(directive).group(1)
                    notes.append((lineno, f"函数式宏 {name}(…) 不在受限子集内"
                                          f"（{c_rules.title_of('WB-C-000')}）"))
                else:
                    m = _OBJ_DEFINE.match(directive)
                    if m:
                        macros[m.group(1)] = _expand(m.group(2).strip(), macros)[0]
                    else:
                        notes.append((lineno, f"无法解析的 #define：{directive[:80]}"))
            elif kind == "undef":
                m = re.search(r"^#\s*undef\s+([A-Za-z_]\w*)", directive)
                if m:
                    macros.pop(m.group(1), None)
            elif kind in _COND_DIRECTIVES:
                notes.append((lineno, f"条件编译 #{kind} 不在受限子集内："
                                      "同一份源码在不同配置下语义不同，交付件必须可复现"))
            elif kind in ("pragma", "line", "error", "warning"):
                notes.append((lineno, f"预处理指令 #{kind} 不在受限子集内"))
            out.extend([""] * (j - i + 1))
            i = j + 1
            continue
        line, in_bc = _expand(line, macros, in_bc)
        out.append(line)
        i += 1
    return "\n".join(out), includes, macros, notes


def parse_file(path: str, source: str, header_preamble: str = "",
               header_lines: int = 0, seed_macros: dict | None = None) -> FileFacts:
    """预处理 + 拼接前导类型与项目头 + 解析。解析失败记录到 facts.parse_error。

    header_preamble 是项目头文件预处理后的文本：pycparser 逐文件解析，源码里
    #include 的头被剥成空行后，头里的 typedef/enum 就不在作用域里，源码会因
    「未知类型 CommCtx」解析失败。把头拼到源文件前面，等价于 gcc 的 #include 语义。
    header_lines 用于把 AST 坐标折算回本文件行号。"""
    facts = FileFacts(path=path)
    text, includes, macros, notes = preprocess(source, seed_macros=seed_macros)
    facts.includes = includes
    facts.macros = {k: v for k, v in macros.items()
                    if k not in PRELUDE_MACROS and k not in (seed_macros or {})}
    facts.notes = notes
    facts.lines = len(source.replace("\r\n", "\n").split("\n"))
    facts.line_offset = PRELUDE_LINES + header_lines
    tu = PRELUDE + header_preamble + text
    try:
        facts.ast = c_parser.CParser().parse(tu, filename=path)
    except Exception as e:                                  # pycparser 抛 ParseError
        facts.parse_error = _fold_line(str(e), facts.line_offset)
    return facts


def _fold_line(msg: str, offset: int = PRELUDE_LINES) -> str:
    """把 pycparser 报错里的 TU 行号折算回原始源码行号。

    报错落在前导类型或头文件前导里（折算后 < 1）时钳到第 1 行：本文件解析不下去，
    指个锚点即可，具体原因看消息文本。"""
    m = re.search(r":(\d+):(\d+)", msg)
    if m:
        real = max(int(m.group(1)) - offset, 1)
        return f"{msg[:m.start()]}:{real}:{m.group(2)}{msg[m.end():]}"
    return msg


def _coord_line(node, offset: int = PRELUDE_LINES, default: int = 0) -> int:
    coord = getattr(node, "coord", None)
    line = getattr(coord, "line", None)
    if not line:
        return default
    return max(line - offset, 1)


# ============================ AST 工具 ============================
def _walk(node):
    """深度优先遍历（含自身）。pycparser 节点的 children() 覆盖所有子节点。"""
    if node is None:
        return
    yield node
    for _, child in node.children():
        yield from _walk(child)


def _is_const_dim(node, enum_consts: set) -> bool:
    """数组维度是否为编译期常量：整型字面量、枚举常量，或由它们组成的算式。"""
    if isinstance(node, c_ast.Constant):
        return node.type in ("int", "unsigned int", "long", "unsigned long",
                             "char", "unsigned char")
    if isinstance(node, c_ast.ID):
        return node.name in enum_consts
    if isinstance(node, c_ast.UnaryOp):
        return _is_const_dim(node.expr, enum_consts)
    if isinstance(node, c_ast.BinaryOp) and node.op in "+-*/%<<>>":
        return (_is_const_dim(node.left, enum_consts)
                and _is_const_dim(node.right, enum_consts))
    if isinstance(node, c_ast.Cast):
        return _is_const_dim(node.expr, enum_consts)
    return False


def _enum_consts(ast) -> set:
    return {n.name for n in _walk(ast)
            if isinstance(n, c_ast.Enumerator) and n.name}


def _has_funcptr(type_node, fp_types: frozenset = frozenset()) -> bool:
    """类型里是否含函数指针（WB-C-003）。

    fp_types 是已识别的函数指针类型别名。pycparser 不展开 typedef：
    `typedef int (*Cb)(int);` 之后写 `Cb h;`，AST 里只剩 IdentifierType('Cb')。
    不解析别名，一层 typedef 就能绕过判据——而回调 typedef 恰恰是模型最常见的写法。
    """
    for n in _walk(type_node):
        if isinstance(n, c_ast.PtrDecl) and isinstance(getattr(n, "type", None),
                                                       c_ast.FuncDecl):
            return True
        if fp_types and isinstance(n, c_ast.IdentifierType):
            if any(name in fp_types for name in (n.names or [])):
                return True
    return False


def _funcptr_typedefs(ast) -> set:
    """收集「本身是函数指针 / 含函数指针成员」的 typedef 名。

    迭代到不再变化：`typedef Cb Cb2;` 这类别名链要顺着已识别的名字再判一轮。
    """
    typedefs = [(t.name, t.type) for t in (getattr(ast, "ext", None) or [])
                if isinstance(t, c_ast.Typedef) and t.name]
    out: set = set()
    for _ in range(len(typedefs) + 1):
        changed = False
        for name, node in typedefs:
            if name not in out and _has_funcptr(node, frozenset(out)):
                out.add(name)
                changed = True
        if not changed:
            break
    return out


def _type_subject(type_node) -> str:
    """给没有变量名的类型定义一个可指认的说法：结论要能对着源码核对。"""
    for n in _walk(type_node):
        if isinstance(n, (c_ast.Struct, c_ast.Union)):
            kind = "结构体" if isinstance(n, c_ast.Struct) else "联合体"
            return f"{kind} {n.name}" if n.name else f"匿名{kind}"
    return "类型定义"


def _loose_type(type_node) -> str:
    """把类型节点压成「基类型 + 指针层数」的宽松签名串。

    宽松是有意的：详细设计里的签名是人写的散文，const / struct 关键字的写法
    常常与代码不一致，逐字符比会制造大量假违规。这里只比真正的接口契约——
    返回什么类型、几个参数、各是什么类型、几层指针。"""
    stars = 0
    node = type_node
    while True:
        if isinstance(node, c_ast.PtrDecl):
            stars += 1
            node = node.type
        elif isinstance(node, c_ast.ArrayDecl):
            stars += 1          # 形参里的 [] 等价于指针
            node = node.type
        elif isinstance(node, c_ast.TypeDecl):
            node = node.type
        elif isinstance(node, c_ast.IdentifierType):
            base = " ".join(node.names)
            break
        elif isinstance(node, (c_ast.Struct, c_ast.Union, c_ast.Enum)):
            base = node.name or "<anonymous>"
            break
        elif isinstance(node, c_ast.FuncDecl):
            return "funcptr"
        else:
            base = "unknown"
            break
    base = re.sub(r"\b(signed)\b\s*", "", base).strip()
    return f"{base}{'*' * stars}"


def _decl_params(func_decl) -> list[str]:
    args = getattr(func_decl, "args", None)
    params = getattr(args, "params", None) or []
    out = []
    for p in params:
        if isinstance(p, c_ast.Typename):        # 匿名参数
            out.append(_loose_type(p.type))
        elif isinstance(p, c_ast.Decl):
            out.append(_loose_type(p.type))
        elif isinstance(p, c_ast.EllipsisParam):
            out.append("...")
        else:
            out.append("?")
    return out


def _loose_from_text(type_text: str) -> str:
    """从散文签名里抽出与 _loose_type 同口径的宽松类型串。"""
    t = (type_text or "").strip()
    if not t:
        return "void"
    stars = t.count("*") + t.count("[")
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"\b(const|volatile|register|struct|union|enum)\b", " ", t)
    toks = [x for x in t.replace("*", " ").split() if x]
    if (len(toks) >= 2 and re.fullmatch(r"[A-Za-z_]\w*", toks[-1])
            and toks[-1] not in _TYPE_WORDS):
        toks = toks[:-1]        # 末位是参数名，丢掉
    base = " ".join(toks) or "void"
    base = re.sub(r"\b(signed)\b\s*", "", base).strip()
    return f"{base}{'*' * stars}"


_SIG_SPLIT = re.compile(r"^(?P<ret>[^()]*?)\s*(?P<name>[A-Za-z_]\w*)\s*\((?P<args>.*)\)\s*;?\s*$",
                        re.DOTALL)


def loose_sig_from_text(sig: str, name_hint: str = "") -> str | None:
    """把详细设计里的签名字符串压成 `ret(p1,p2)` 形式；解析不出返回 None。"""
    text = (sig or "").strip().rstrip(";").strip()
    if not text:
        return None
    depth, cut = 0, -1
    for idx, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                cut = idx
            depth += 1
        elif ch == ")":
            depth -= 1
    if cut < 0:
        return None
    head, args = text[:cut].strip(), text[cut + 1:]
    depth = 0
    while args.endswith(")") and depth == 0:
        args = args[:-1]
        depth = 1
    head_toks = head.split()
    if head_toks and (head_toks[-1] == name_hint or re.fullmatch(r"[A-Za-z_]\w*", head_toks[-1])):
        ret_part = " ".join(head_toks[:-1])
    else:
        ret_part = head
    params = _split_args(args)
    if params == ["void"] or params == [""]:
        params = []
    ret = _loose_from_text(ret_part) if ret_part.strip() else "int"
    return ret + "(" + ",".join(_loose_from_text(p) for p in params) + ")"


def _split_args(args: str) -> list[str]:
    out, depth, cur = [], 0, []
    for ch in args:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def loose_sig_from_ast(func_decl) -> str:
    """代码侧的宽松签名，与 loose_sig_from_text 同口径。"""
    # 调用方给的可能是 Decl（其 .type 才是 FuncDecl），也可能直接是 FuncDecl。
    # 不剥这一层会把函数返回类型算成 funcptr，进而造出成片的假 WB-D-002。
    while not isinstance(func_decl, c_ast.FuncDecl):
        inner = getattr(func_decl, "type", None)
        if inner is None:
            break
        func_decl = inner
    ret = _loose_type(func_decl.type)
    params = _decl_params(func_decl)
    if params == ["void"]:
        params = []
    return ret + "(" + ",".join(params) + ")"


# ============================ 单文件检查 ============================
def _const_object(type_node) -> bool:
    """该对象是否只读。

    指针要单独看：`const uint8_t *p` 是「指向常量的可变指针」，指针本身仍可改写，
    所以顶层是 PtrDecl 时只看 PtrDecl 自己的 const 限定。"""
    if isinstance(type_node, c_ast.PtrDecl):
        return "const" in (type_node.quals or [])
    return any(isinstance(n, c_ast.TypeDecl) and "const" in (n.quals or [])
               for n in _walk(type_node))


def _allocates_storage(type_node) -> bool:
    """这个声明是否真的占用了可写存储。

    `enum { MAX = 32 };` 只引入编译期常量，不分配任何对象；按可写全局判会造出
    成片假 WB-C-007，而定长数组维度恰恰推荐用枚举常量表达（见 WB-C-004 判据）。"""
    node = type_node
    while isinstance(node, (c_ast.PtrDecl, c_ast.ArrayDecl, c_ast.TypeDecl)):
        node = node.type
    return not isinstance(node, (c_ast.Enum, c_ast.FuncDecl))


def _is_prototype(decl) -> bool:
    """是否为函数原型声明（`int f(int);`）。

    不能把 PtrDecl 剥掉：`int (*cb)(int);` 剥完看着也像原型，实际是函数指针变量。
    两者判据不同——原型只查参数里有没有函数指针，变量本身就是违规，
    混为一谈会让结论的措辞与定位都指错。
    """
    t = decl.type
    while isinstance(t, c_ast.ArrayDecl):
        t = t.type
    return isinstance(t, c_ast.FuncDecl)


def check_facts(facts: FileFacts, opts: dict) -> tuple[list, list]:
    """检查一个已解析的文件，返回 (violations, function_facts)。"""
    v: list[Violation] = [
        Violation(rule_id="WB-C-000", file=facts.path, line=ln, message=msg)
        for ln, msg in facts.notes
    ]
    funcs: list[dict] = []
    if facts.ast is None:
        v.append(Violation(
            rule_id="WB-C-000", file=facts.path, line=_parse_error_line(facts.parse_error),
            message=f"源码无法按受限子集解析：{facts.parse_error}"))
        return v, funcs

    ast = facts.ast
    enum_consts = _enum_consts(ast)
    fp_types = frozenset(_funcptr_typedefs(ast))
    cmax = int(opts.get("complexity_max") or 10)
    off = facts.line_offset

    # ---- typedef：函数指针类型别名本身就是「引入函数指针」----
    for ext in ast.ext:
        td = ext if isinstance(ext, c_ast.Typedef) else getattr(ext, "type", None)
        if not isinstance(td, c_ast.Typedef) or not td.name:
            continue
        if _has_funcptr(td.type):
            v.append(Violation(
                "WB-C-003", facts.path, _coord_line(td, off, 1),
                f"typedef {td.name} 引入了函数指针类型；需要分派时用枚举 + switch",
                func=td.name))

    # ---- 文件作用域：可写全局变量 / 函数指针 / 数组定长 ----
    for ext in ast.ext:
        if not isinstance(ext, c_ast.Decl) or "typedef" in (ext.storage or []):
            continue
        if isinstance(ext.type, c_ast.Typedef):
            continue
        line = _coord_line(ext, off, 1)
        if _is_prototype(ext):
            if _has_funcptr(ext.type, fp_types):
                v.append(Violation("WB-C-003", facts.path, line,
                                   f"函数原型 {ext.name} 使用了函数指针", func=ext.name or ""))
            continue
        if _has_funcptr(ext.type, fp_types):
            v.append(Violation(
                "WB-C-003", facts.path, line,
                f"文件作用域声明 {ext.name or _type_subject(ext.type)} 使用了函数指针",
                func=ext.name or ""))
            continue
        if not ext.name or not _allocates_storage(ext.type):
            continue        # 匿名 enum / struct 定义，不占用可写存储
        if not _const_object(ext.type):
            v.append(Violation(
                "WB-C-007", facts.path, line,
                f"文件作用域变量 {ext.name} 可写（{'、'.join(ext.storage or ['(外部链接)'])}）；"
                f"模块状态应收敛到显式传入的上下文结构体"))
        for arr in (n for n in _walk(ext.type) if isinstance(n, c_ast.ArrayDecl)):
            if not _is_const_dim(arr.dim, enum_consts):
                v.append(Violation(
                    "WB-C-004", facts.path, line,
                    f"文件作用域数组 {ext.name} 维度不是编译期常量"))
                break

    # ---- 函数定义 ----
    for ext in ast.ext:
        if not isinstance(ext, c_ast.FuncDef):
            continue
        decl = ext.decl
        name = decl.name or "<anonymous>"
        line = _coord_line(decl, off, 1)
        storage = decl.storage or []
        is_static = "static" in storage
        params = _decl_params(decl.type)
        body_nodes = list(_walk(ext.body))
        returns = sum(1 for n in body_nodes if isinstance(n, c_ast.Return))
        complexity = _complexity(ext.body)
        calls = sorted({n.name.name for n in body_nodes
                        if isinstance(n, c_ast.FuncCall) and isinstance(n.name, c_ast.ID)})
        funcs.append({"name": name, "file": facts.path, "line": line,
                      "static": is_static, "ret": _loose_type(decl.type.type),
                      "params": params, "sig": loose_sig_from_ast(decl),
                      "complexity": complexity, "returns": returns, "calls": calls})

        if _has_funcptr(decl.type, fp_types):
            v.append(Violation("WB-C-003", facts.path, line,
                               f"函数 {name} 的参数或返回类型含函数指针", func=name))

        for n in body_nodes:
            if isinstance(n, c_ast.FuncCall) and isinstance(n.name, c_ast.ID):
                if n.name.name in c_rules.DYNAMIC_ALLOC_FUNCS:
                    v.append(Violation(
                        "WB-C-001", facts.path, _coord_line(n, off, line),
                        f"函数 {name} 调用了动态内存接口 {n.name.name}()", func=name))
            elif isinstance(n, c_ast.Decl):
                st = n.storage or []
                if "static" in st and not _const_object(n.type):
                    v.append(Violation(
                        "WB-C-007", facts.path, _coord_line(n, off, line),
                        f"函数 {name} 内的 static 变量 {n.name} 可写，构成隐式全局状态",
                        func=name))
                if _has_funcptr(n.type, fp_types):
                    v.append(Violation(
                        "WB-C-003", facts.path, _coord_line(n, off, line),
                        f"函数 {name} 内声明了函数指针 {n.name}", func=name))
                if not isinstance(n.type, c_ast.FuncDecl):
                    for arr in (a for a in _walk(n.type) if isinstance(a, c_ast.ArrayDecl)):
                        if not _is_const_dim(arr.dim, enum_consts):
                            v.append(Violation(
                                "WB-C-004", facts.path, _coord_line(n, off, line),
                                f"函数 {name} 内的数组 {n.name} 是变长数组（VLA）"
                                f"或维度不是编译期常量", func=name))
                            break

        if returns > 1:
            v.append(Violation("WB-C-005", facts.path, line,
                               f"函数 {name} 有 {returns} 个 return 语句，建议合并为单出口",
                               func=name))
        if complexity > cmax:
            v.append(Violation(
                "WB-C-006", facts.path, line,
                f"函数 {name} 圈复杂度 {complexity}，超过上限 {cmax}", func=name))

    # ---- 形参里的 buf[] 是合法的（等价指针），单独按宽松口径扫一遍 ----
    return v, funcs


def _complexity(body) -> int:
    """圈复杂度 = 1 + 判定点数（if / for / while / do / case / && / || / ?:）。"""
    n = 1
    for node in _walk(body):
        if isinstance(node, (c_ast.If, c_ast.For, c_ast.While, c_ast.DoWhile,
                             c_ast.TernaryOp)):
            n += 1
        elif isinstance(node, c_ast.Case) and node.expr is not None:
            n += 1          # default 不引入新路径，不计
        elif isinstance(node, c_ast.BinaryOp) and node.op in ("&&", "||"):
            n += 1
    return n


def _parse_error_line(msg: str) -> int:
    m = re.search(r":(\d+):", msg or "")
    return int(m.group(1)) if m else 1


# ============================ 递归检测（跨函数） ============================
def find_recursion(funcs: list[dict]) -> list[list[str]]:
    """在调用图里找回环。返回若干条环路径（首尾同名的列表）。

    直接递归与间接递归都要抓：航天嵌入式里间接递归更难被评审看出来，
    而栈溢出的后果是一样的。"""
    graph = {f["name"]: set(f["calls"]) for f in funcs}
    known = set(graph)
    cycles: list[list[str]] = []
    seen_cycles: set = set()

    def dfs(node: str, stack: list[str], onstack: set):
        for nxt in sorted(graph.get(node, ()) & known):
            if nxt in onstack:
                idx = stack.index(nxt)
                cyc = stack[idx:] + [nxt]
                key = tuple(sorted(set(cyc)))
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    cycles.append(cyc)
                continue
            onstack.add(nxt)
            stack.append(nxt)
            dfs(nxt, stack, onstack)
            stack.pop()
            onstack.discard(nxt)

    for name in sorted(known):
        dfs(name, [name], {name})
    return cycles


# ============================ 设计覆盖检查 ============================
def design_coverage(lld_functions, funcs: list[dict]) -> list[Violation]:
    """详细设计 functions 清单 ↔ 代码实现的对照（WB-D-001/002/003）。"""
    v: list[Violation] = []
    designed = [f for f in (lld_functions or []) if isinstance(f, dict) and f.get("name")]
    if not designed:
        return v
    index = {f["name"]: f for f in funcs}
    designed_names = {str(f["name"]).strip() for f in designed}

    for f in designed:
        name = str(f["name"]).strip()
        impl = index.get(name)
        if impl is None:
            v.append(Violation(
                "WB-D-001", impl_file(funcs), 0,
                f"详细设计要求的函数 {name} 在代码中未实现", func=name))
            continue
        want = loose_sig_from_text(str(f.get("sig") or ""), name_hint=name)
        got = impl.get("sig")
        if want and got and want != got:
            v.append(Violation(
                "WB-D-002", impl["file"], impl["line"],
                f"函数 {name} 签名与详细设计不一致：设计「{f.get('sig')}」→ {want}，"
                f"实现 → {got}", func=name))

    for f in funcs:
        if not f["static"] and f["name"] not in designed_names and f["name"] != "main":
            v.append(Violation(
                "WB-D-003", f["file"], f["line"],
                f"公开函数 {f['name']} 未出现在详细设计 functions 清单中", func=f["name"]))
    return v


def impl_file(funcs: list[dict]) -> str:
    """WB-D-001 没有具体文件可指，取第一个实现文件做定位锚点。"""
    for f in funcs:
        if f.get("file"):
            return f["file"]
    return "-"


# ============================ 汇总报告 ============================
def split_files(files: dict) -> tuple[dict, dict]:
    """把 {路径: 内容} 分成源文件与其他资源（头文件也参与解析，但不单独出结论）。"""
    sources, others = {}, {}
    for path, content in (files or {}).items():
        p = str(path).replace("\\", "/")
        if p.endswith(".c"):
            sources[p] = content
        else:
            others[p] = content
    return sources, others


def _header_preamble(others: dict) -> tuple[str, int, dict]:
    """把项目头文件预处理成可拼到每个源文件前面的前导文本。

    返回 (前导文本, 前导行数, 头里的宏表)。pycparser 逐文件解析，源码 #include 的
    头被剥成空行后，头里的 typedef/enum 就不在作用域里，源码会因「未知类型」解析
    失败；把头拼到源文件前面即等价于 gcc 的 #include 语义。头里的 include guard
    （#ifndef/#define/#endif）是头的正常写法，其 notes 不计入源文件违规，故只取文本与宏。"""
    texts: list[str] = []
    macros: dict = {}
    for hpath in sorted(p for p in (others or {}) if str(p).endswith(".h")):
        htext, _inc, hmacros, _notes = preprocess(str(others[hpath]))
        texts.append(htext)
        macros.update(hmacros)
    preamble = ("\n".join(texts) + "\n") if texts else ""
    return preamble, preamble.count("\n"), macros


def check_files(files: dict, opts: dict | None = None,
                lld_functions=None) -> dict:
    """静态检查主入口：返回可直接落库为产物的报告字典。"""
    opts = dict(opts or {})
    sources, others = split_files(files)
    header_preamble, header_lines, header_macros = _header_preamble(others)
    violations: list[Violation] = []
    funcs: list[dict] = []
    parsed: list[dict] = []

    for path in sorted(sources):
        facts = parse_file(path, str(sources[path]),
                           header_preamble=header_preamble,
                           header_lines=header_lines,
                           seed_macros=header_macros)
        fv, ff = check_facts(facts, opts)
        violations.extend(fv)
        funcs.extend(ff)
        parsed.append({"path": path, "lines": facts.lines,
                       "includes": facts.includes, "macros": len(facts.macros),
                       "parse_error": facts.parse_error})

    for cyc in find_recursion(funcs):
        first = cyc[0]
        anchor = next((f for f in funcs if f["name"] == first), {})
        violations.append(Violation(
            "WB-C-002", anchor.get("file", "-"), anchor.get("line", 0),
            f"检测到递归调用环：{' → '.join(cyc)}", func=first))

    violations.extend(design_coverage(lld_functions, funcs))
    violations.sort(key=lambda x: x.sort_key())

    by_rule: dict[str, int] = {}
    for x in violations:
        by_rule[x.rule_id] = by_rule.get(x.rule_id, 0) + 1
    required = [x for x in violations if x.blocking]
    advisory = [x for x in violations if not x.blocking]
    waivable_req = [x for x in required if x.waivable]

    return {
        "ok": not required,
        "total": len(violations),
        "required": len(required),
        "advisory": len(advisory),
        "waivable_required": len(waivable_req),
        "non_waivable_required": len(required) - len(waivable_req),
        "by_rule": by_rule,
        "violations": [x.to_dict() for x in violations],
        "functions": funcs,
        "files": parsed,
        "thresholds": {"complexity_max": int(opts.get("complexity_max") or 10)},
        "rule_table": c_rules.as_dicts(),
    }


def feedback_text(report: dict, limit: int = 12) -> str:
    """把必查项违规整理成回灌给代码生成智能体的整改要求。

    回灌要带规则编号与判据原文：模型只有知道「为什么不行」才改得对，
    只给一句「静态检查失败」等于让它猜。"""
    req = [x for x in report.get("violations") or []
           if x.get("severity") == c_rules.SEV_REQUIRED]
    if not req:
        return ""
    lines = [f"静态检查发现 {len(req)} 处必查项违规，必须整改后重新输出完整代码："]
    for x in req[:limit]:
        rule = c_rules.get(x["rule_id"])
        lines.append(f"- [{x['rule_id']} {x['title']}] {x['file']}:{x['line']}"
                     + (f" 函数 {x['func']}" if x.get("func") else "")
                     + f" — {x['message']}")
        if rule:
            cmax = report.get("thresholds", {}).get("complexity_max", 10)
            lines.append(f"  判据要求：{c_rules.render(rule.prompt, cmax)}")
    if len(req) > limit:
        lines.append(f"（另有 {len(req) - limit} 处同类违规，一并整改）")
    return "\n".join(lines)


def markdown_report(report: dict, title: str = "静态检查报告") -> str:
    """把报告渲染成 Markdown，作为 static 阶段产物落库（前端直接展示）。"""
    verdict = "通过（无必查项违规）" if report.get("ok") else "不通过（存在必查项违规）"
    lines = [f"# {title}", "",
             f"> 结论：**{verdict}**　必查项 {report.get('required', 0)} 条 · "
             f"建议项 {report.get('advisory', 0)} 条", ""]
    by_rule = report.get("by_rule") or {}
    if by_rule:
        lines += ["## 违规分布", "", "| 规则编号 | 判据 | 等级 | 条数 |",
                  "|---|---|---|---|"]
        for rid, cnt in sorted(by_rule.items()):
            rule = c_rules.get(rid)
            lines.append(f"| {rid} | {rule.title if rule else rid} | "
                         f"{c_rules.SEVERITY_TEXT.get(c_rules.severity_of(rid), '-')} | {cnt} |")
        lines.append("")
    vs = report.get("violations") or []
    if vs:
        lines += ["## 违规明细", "", "| 规则 | 文件 | 行 | 函数 | 说明 |",
                  "|---|---|---|---|---|"]
        for x in vs:
            msg = str(x.get("message", "")).replace("|", "/").replace("\n", " ")
            lines.append(f"| {x['rule_id']} | {x['file']} | {x['line'] or '-'} | "
                         f"{x.get('func') or '-'} | {msg} |")
        lines.append("")
    funcs = report.get("functions") or []
    if funcs:
        lines += ["## 函数清单（含圈复杂度）", "",
                  "| 函数 | 文件 | 行 | 链接 | 圈复杂度 | return 数 |",
                  "|---|---|---|---|---|---|"]
        for f in funcs:
            lines.append(f"| {f['name']} | {f['file']} | {f['line']} | "
                         f"{'static' if f['static'] else 'extern'} | "
                         f"{f['complexity']} | {f['returns']} |")
        lines.append("")
    lines += ["## 判据来源", "", c_rules.rule_table_markdown(), "",
              "> 条款号待标准化部门核定后填入；规则编号与判定行为本期冻结。"]
    return "\n".join(lines)
