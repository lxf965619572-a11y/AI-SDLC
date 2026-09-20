"""map-reduce 抽取：分块分批送 LLM 抽取业务对象/规则/流程，再合并去重。
map 阶段按 BATCH_SIZE 个分块合并为一次 LLM 调用（大幅减少调用次数），
线程池并发执行，progress_cb 上报进度；map 结果落盘缓存，重跑可复用。
reduce 阶段分批归并且对 LLM 失败兜底（本地按名称去重合并），保证不中断流水线。"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import config
from core import llm_client
from core.json_utils import extract_json
from core.trace import SRC_PREFIXES, assign_ids, norm_source_tags

MAP_CONCURRENCY = config.MAP_CONCURRENCY  # 并发批次数（环境变量 MAP_CONCURRENCY 可调）
BATCH_SIZE = 10       # map 每批合并的分块数
REDUCE_BATCH_SIZE = 8  # reduce 每批归并的份数（避免单次输入/输出过大导致超时）

CACHE_DIR = config.DATA_DIR / "parse_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MAP_PROMPT = """你是需求文档分析专家。请从以下需求文档的多个片段中抽取结构化信息。__MAP__
每个片段开头都带有片段编号（如 D1C3，表示第 1 份文档的第 3 段）。

只输出 JSON，格式：
{
  "objects": [{"name": "业务对象名", "attrs": ["属性1", "属性2"], "desc": "一句话说明", "source": ["D1C3"]}],
  "rules": [{"name": "规则名", "desc": "规则内容", "source": ["D1C3"]}],
  "flows": [{"name": "流程名", "steps": ["步骤1", "步骤2"], "source": ["D2C1"]}]
}
source 填该条信息出自哪些片段编号，只能使用下面出现过的编号，不得编造；
同一条信息出自多个片段时全部列出。片段中没有的项给空数组，不要编造。

文档片段：
{text}
"""

REDUCE_PROMPT = """你是需求文档分析专家。以下是从同一份需求文档多个片段中分别抽取的结构化信息（JSON 数组）。
请合并去重：同一业务对象合并属性；语义相同的规则/流程合并；保持信息完整。
合并时必须保留 source 字段：同一项被合并时，把各份的 source 片段编号取并集，不要丢弃。

只输出 JSON，格式：
{
  "objects": [{"name": "...", "attrs": ["..."], "desc": "...", "source": ["..."]}],
  "rules": [{"name": "...", "desc": "...", "source": ["..."]}],
  "flows": [{"name": "...", "steps": ["..."], "source": ["..."]}]
}

待合并的抽取结果：
{parts}
"""


def _merge_lists(merged: dict, part: dict):
    """简单兜底合并：按 name 去重，同名项合并属性与来源片段。"""
    for key in ("objects", "rules", "flows"):
        index = {o.get("name"): o for o in merged.get(key, [])}
        for o in part.get(key, []):
            name = o.get("name")
            if not name:
                continue
            if name not in index:
                merged.setdefault(key, []).append(o)
                index[name] = o
                continue
            prev = index[name]
            for field in ("attrs", "steps", "source"):
                extra = [x for x in (o.get(field) or []) if x not in (prev.get(field) or [])]
                if extra:
                    prev[field] = list(prev.get(field) or []) + extra
            if not prev.get("desc") and o.get("desc"):
                prev["desc"] = o["desc"]


def _local_merge(parts: list[dict]) -> dict:
    """本地兜底合并（不调用 LLM）：按名称去重合并。"""
    merged: dict = {"objects": [], "rules": [], "flows": []}
    for p in parts:
        _merge_lists(merged, p)
    return merged


def _cache_path(text: str) -> Path:
    # key 带上模型来源：mock 跑过的文档切回真实模型时不能命中那份假数据
    flavor = config.llm_cache_flavor("extraction")
    key = f"{flavor}::{text}"
    return CACHE_DIR / (hashlib.md5(key.encode("utf-8")).hexdigest() + ".json")


def _tag_chunks(chunks: list[dict]) -> list[dict]:
    """给分块打稳定来源标记：D{文档序号}C{段序号}；拿不到文档名时退化为 C{全局序号}。"""
    docs: list[str] = []
    for i, ch in enumerate(chunks, 1):
        name = ch.get("doc") or ""
        if name:
            if name not in docs:
                docs.append(name)
            ch["tag"] = f"D{docs.index(name) + 1}C{ch.get('seq', i)}"
        else:
            ch["tag"] = f"C{i}"
    return chunks


def _llm_reduce(batch: list[dict], progress_cb=None, tag: str = "",
                valid_tags: set | None = None) -> dict:
    """调用 LLM 合并一批抽取结果；失败/解析失败时兜底本地合并（不抛异常）。"""
    try:
        if progress_cb:
            progress_cb(f"reduce{tag}：合并 {len(batch)} 份结果…")
        raw = llm_client.chat(
            [{"role": "user",
              "content": REDUCE_PROMPT.replace("{parts}", json.dumps(batch, ensure_ascii=False))}],
            role="extraction", response_json=True,
        )
        merged = extract_json(raw)
        if merged:
            if valid_tags is not None:
                merged = _clean_sources(merged, valid_tags)
            return merged
    except Exception:
        pass
    return _local_merge(batch)


def _extract_batch(batch: list[dict]) -> tuple[dict | None, bool]:
    """抽取一批分块（合并成一次 LLM 调用），返回 (结构化 dict 或 None, 是否缓存命中)。
    结果按批内容哈希落盘缓存，重跑时直接命中。"""
    tagged, plain = [], []
    for ch in batch:
        heading = f"标题：{ch['heading']}" if ch.get("heading") else "无标题"
        tag = ch.get("tag") or "C0"
        tagged.append(f"--- 片段 {tag}（{heading}）---\n{ch['content']}")
        plain.append(f"--- 片段（{heading}）---\n{ch['content']}")
    text = "\n\n".join(tagged)
    valid_tags = {ch.get("tag") for ch in batch if ch.get("tag")}

    cache = _cache_path(text)
    # 旧格式（片段不带编号）的缓存仍然复用：省掉重跑的抽取开销，只是缺来源标记
    for path in (cache, _cache_path("\n\n".join(plain))):
        if path.exists():
            try:
                return _clean_sources(json.loads(path.read_text(encoding="utf-8")),
                                      valid_tags), True
            except Exception:
                pass

    prompt = MAP_PROMPT.replace("{text}", text)
    try:
        raw = llm_client.chat(
            [{"role": "user", "content": prompt}],
            role="extraction", response_json=True,
        )
        obj = extract_json(raw)
    except Exception:
        obj = None
    if obj:
        obj = _clean_sources(obj, valid_tags)
        try:
            cache.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return obj, False


def _clean_sources(obj, valid_tags: set) -> dict:
    """把 LLM 给出的 source 规范成本批真实存在的片段编号，丢弃编造项。"""
    if not isinstance(obj, dict):
        return obj
    for key in SRC_PREFIXES:
        for item in obj.get(key) or []:
            if not isinstance(item, dict):
                continue
            srcs = [s for s in norm_source_tags(item.get("source")) if s in valid_tags]
            item["source"] = srcs
    return obj


def extract_structured(chunks: list[dict], progress_cb=None, reduce_cb=None,
                       project_id: int | None = None) -> dict:
    """chunks: [{"seq","heading","content"}] → {"objects","rules","flows"}。
    map 阶段按 BATCH_SIZE 分批并发；progress_cb(done, total) 上报进度；
    reduce_cb(message) 上报归并阶段消息。传入 project_id 时支持取消。"""
    from core import cancel

    if not chunks:
        return assign_ids({"objects": [], "rules": [], "flows": []})

    _tag_chunks(chunks)
    all_tags = {ch.get("tag") for ch in chunks if ch.get("tag")}
    batches = [chunks[i:i + BATCH_SIZE] for i in range(0, len(chunks), BATCH_SIZE)]
    total = len(batches)
    results = [None] * total
    done_count = 0
    cancelled = False
    with ThreadPoolExecutor(max_workers=MAP_CONCURRENCY) as pool:
        futures = {pool.submit(_extract_batch, b): i for i, b in enumerate(batches)}
        for fut in as_completed(futures):
            if project_id is not None and cancel.is_cancelled(project_id):
                cancelled = True
                for f in futures:
                    f.cancel()
                break
            idx = futures[fut]
            cached = False
            try:
                results[idx], cached = fut.result()
            except Exception:
                results[idx] = None
            done_count += 1
            if progress_cb:
                progress_cb(done_count, total, cached)

    if cancelled:
        raise InterruptedError("用户取消了流水线")

    parts = [r for r in results if r]
    if not parts:
        return assign_ids({"objects": [], "rules": [], "flows": []})
    if len(parts) == 1:
        return assign_ids(parts[0])

    # reduce：分批归并（每批最多 REDUCE_BATCH_SIZE 份），逐层收敛；LLM 失败自动本地兜底
    level = 0
    while len(parts) > REDUCE_BATCH_SIZE:
        level += 1
        nxt = []
        for i in range(0, len(parts), REDUCE_BATCH_SIZE):
            batch = parts[i:i + REDUCE_BATCH_SIZE]
            if len(batch) == 1:
                nxt.append(batch[0])
            else:
                nxt.append(_llm_reduce(batch, reduce_cb,
                                       tag=f" L{level}-{i // REDUCE_BATCH_SIZE + 1}",
                                       valid_tags=all_tags))
        parts = nxt
    return assign_ids(_llm_reduce(parts, reduce_cb, tag=" 最终", valid_tags=all_tags))


def render_markdown(structured: dict, doc_names: list[str]) -> str:
    """把结构化数据渲染为《结构化原始数据》Markdown。"""
    lines = ["# 结构化原始数据", ""]
    if doc_names:
        lines += ["> 来源文档：" + "、".join(doc_names), ""]
        lines += ["> 编号说明：素材编号 OBJ/RULE/FLOW-xxx 供下游需求引用；"
                  "来源标记 D{n}C{m} 表示第 n 份文档的第 m 段。", ""]

    lines += ["## 一、业务对象", "", "| 编号 | 对象 | 属性 | 说明 | 来源 |",
              "|---|---|---|---|---|"]
    for o in structured.get("objects", []):
        attrs = "、".join(o.get("attrs", [])) or "-"
        lines.append(f"| {o.get('id','-')} | {o.get('name','-')} | {attrs} | "
                     f"{o.get('desc','-')} | {'、'.join(o.get('source') or []) or '-'} |")

    lines += ["", "## 二、业务规则", "", "| 编号 | 规则 | 内容 | 来源 |", "|---|---|---|---|"]
    for r in structured.get("rules", []):
        lines.append(f"| {r.get('id','-')} | {r.get('name','-')} | {r.get('desc','-')} | "
                     f"{'、'.join(r.get('source') or []) or '-'} |")

    lines += ["", "## 三、业务流程", "", "| 编号 | 流程 | 步骤 | 来源 |", "|---|---|---|---|"]
    for f in structured.get("flows", []):
        steps = " → ".join(f.get("steps", []))
        lines.append(f"| {f.get('id','-')} | {f.get('name','-')} | {steps} | "
                     f"{'、'.join(f.get('source') or []) or '-'} |")

    lines += ["", "```json", json.dumps(structured, ensure_ascii=False, indent=2), "```"]
    return "\n".join(lines)
