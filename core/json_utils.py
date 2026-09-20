"""LLM 输出清洗与 JSON 解析（借鉴 spec2case 的健壮性实践）。"""
import json
import re

# 围栏代码块：group(1) = 语言标记（裸围栏时为空），group(2) = 块体。
# 语言标记必须单独捕获：否则 ```sql / ```c 的标记会被吞进块体，
# 让非 JSON 代码块混进元数据候选。
_FENCE_BLOCK = re.compile(r"```([\w+-]*)[ \t\r]*\n?([\s\S]*?)```", re.IGNORECASE)


def strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 包裹，保留内部内容。"""
    m = _FENCE_BLOCK.search(text)
    if m:
        return m.group(2).strip()
    return text.strip()


def extract_json(text: str):
    """从 LLM 输出中提取第一个合法 JSON（对象或数组）。失败返回 None。"""
    if text is None:
        return None
    candidates = []
    for m in _FENCE_BLOCK.finditer(text):
        candidates.append(m.group(2))
    candidates.append(text)
    for cand in candidates:
        cand = cand.strip()
        obj = _try_parse(cand)
        if obj is not None:
            return obj
        # 尝试截取首个 { ... } 或 [ ... ] 平衡片段
        frag = _balanced_fragment(cand)
        if frag:
            obj = _try_parse(frag)
            if obj is not None:
                return obj
    return None


def _try_parse(s: str):
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def _balanced_fragment(s: str) -> str | None:
    """截取从第一个 {/[ 起的平衡括号片段。"""
    start_obj, start_arr = s.find("{"), s.find("[")
    starts = [i for i in (start_obj, start_arr) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    opener, closer = ("{", "}") if s[start] == "{" else ("[", "]")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def split_markdown_and_meta(text: str) -> tuple[str, dict | None]:
    """把『Markdown 文档 + ```json 元数据块』拆分为 (markdown, meta)。

    元数据块不要求一定位于文档末尾：概要设计会在其后附 ```sql DDL，
    详细设计会附 ```c 代码块。因此从后往前扫描，跳过明确标注为其它语言的
    围栏块，取第一个能解析为 JSON 对象的块作为元数据。
    找不到时返回 (原文, None)，由调用方触发重试。
    """
    for m in reversed(list(_FENCE_BLOCK.finditer(text))):
        lang = m.group(1).lower()
        if lang and lang != "json":
            continue
        meta = _try_parse(m.group(2).strip())
        if not isinstance(meta, dict):
            continue
        before = text[:m.start()].strip()
        after = text[m.end():].strip()
        markdown = f"{before}\n\n{after}" if (before and after) else (before or after)
        return markdown, meta
    return text.strip(), None
