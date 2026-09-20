"""生成物里的 C 文件抽取与回渲染。

第一性原理：代码阶段的产物是「若干份源文件」，不是一篇文档。但把源码塞进
JSON 元数据是错的路子——C 代码里的引号、反斜杠、换行全要转义，模型转义错一处
整份元数据就解析不出来，而且流式过程中前端看到的是满屏 \\n 的天书。

所以约定：源码留在 Markdown 的围栏代码块里（人能读、能流式展示、能直接评审），
末尾的 ```json 元数据只登记「有哪些文件、每个函数实现哪条需求」。本模块负责在
两者之间做抽取与回渲染，是唯一的路径↔内容转换点。

文件识别约定（提示词里也是这么写的）：
    ## 文件 src/comm.c        ← 代码块之前最近的标题/说明行里出现路径
    ```c
    ...
    ```
也容忍把路径写进围栏信息串（```c src/comm.c）。
"""
from __future__ import annotations

import re

# 路径 token：只认约定目录下的 .c/.h，避免把正文里提到的 src 目录误判成文件
_PATH_RE = re.compile(
    r"(?<![\w./-])((?:src|include|tests)/[A-Za-z0-9_./-]*[A-Za-z0-9_-]\.[chCH])(?![\w])")

_FENCE_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>.*?)[ \t]*$")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]")

# 允许同步到验证机的目录前缀（与 buildkit 的目录约定一致）
ALLOWED_ROOTS = ("src/", "include/", "tests/")


def path_in(text: str) -> str:
    """取一行里出现的第一个约定路径；没有则返回空串。"""
    m = _PATH_RE.search(text or "")
    return m.group(1) if m else ""


def _dedent(text: str) -> str:
    """去掉代码块的公共缩进。

    模型把代码块嵌在列表或引用里时会整体缩进，直接编译不影响，但落进交付件
    和证据归档里就很难看，且会让静态检查报出的行号与评审看到的对不上。"""
    lines = text.split("\n")
    widths = [len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()]
    if not widths:
        return text.strip("\n")
    n = min(widths)
    if n <= 0:
        return text.strip("\n")
    return "\n".join(ln[n:] if ln.strip() else ln for ln in lines).strip("\n")


def _is_close(line: str, fence: str) -> bool:
    s = line.strip()
    ch = fence[0]
    return len(s) >= len(fence) and set(s) == {ch}


def extract_files(markdown: str) -> dict:
    """从生成物里抽出 {路径: 内容}。同一路径出现多次则按顺序拼接（分块输出的情况）。"""
    out: dict[str, list[str]] = {}
    lines = (markdown or "").split("\n")
    pending = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group("fence")
            path = path_in(m.group("info")) or pending
            body: list[str] = []
            i += 1
            while i < len(lines) and not _is_close(lines[i], fence):
                body.append(lines[i])
                i += 1
            i += 1                       # 跳过收尾围栏
            if path:
                out.setdefault(path, []).extend(body)
            pending = ""
            continue
        tok = path_in(line)
        if tok:
            pending = tok
        elif _HEADING_RE.match(line):
            pending = ""                 # 新标题没带路径：上一个路径不再适用
        i += 1
    return {p: _dedent("\n".join(b)) for p, b in out.items() if "".join(b).strip()}


def render_files(files: dict, title: str = "代码实现",
                 intro: list | None = None, lang: str = "c") -> str:
    """把 {路径: 内容} 渲染回本模块约定的 Markdown（mock 模式与回写归一化用）。"""
    lines = [f"# {title}", ""]
    lines += [str(x) for x in (intro or [])]
    if intro:
        lines.append("")
    for path in sorted(files):
        head = "h" if path.endswith(".h") else lang
        lines += [f"## 文件 {path}", "", f"```{head}", str(files[path]).rstrip("\n"),
                  "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def file_list(files: dict) -> list:
    """元数据里登记的相对路径清单（排序，便于比对与展示）。"""
    return sorted(str(p).replace("\\", "/") for p in (files or {}))


def illegal_paths(files: dict) -> list:
    """不在约定目录下的路径：这些文件同步到验证机也不会被 build.sh 编译。"""
    bad = []
    for p in (files or {}):
        q = str(p).replace("\\", "/")
        if not q.startswith(ALLOWED_ROOTS) or q.startswith("/") or ".." in q.split("/"):
            bad.append(q)
    return sorted(bad)
