"""LLM 输出清洗与 JSON 解析（借鉴 spec2case 的健壮性实践）。"""
import json
import re

_JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 包裹，保留内部内容。"""
    m = _JSON_BLOCK.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def extract_json(text: str):
    """从 LLM 输出中提取第一个合法 JSON（对象或数组）。失败返回 None。"""
    if text is None:
        return None
    candidates = []
    for m in _JSON_BLOCK.finditer(text):
        candidates.append(m.group(1))
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
    """把『Markdown 文档 + 末尾 ```json 元数据块』拆分为 (markdown, meta)。"""
    blocks = list(_JSON_BLOCK.finditer(text))
    meta = None
    if blocks:
        last = blocks[-1]
        parsed = _try_parse(last.group(1).strip())
        if parsed is not None:
            meta = parsed
            markdown = text[:last.start()].strip()
            trailing = text[last.end():].strip()
            if trailing:
                markdown += "\n\n" + trailing
            return markdown, meta
    return text.strip(), None
