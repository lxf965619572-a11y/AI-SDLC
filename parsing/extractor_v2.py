"""重构后的 extractor.py：增强错误处理和可观测性。

主要改进：
1. 明确标记降级策略的使用
2. 记录错误到追踪器
3. 返回 ExtractionResult 而非裸数据
4. 区分"缓存命中"和"降级到本地合并"
5. 使用并发安全的 CacheManager（原子写入+文件锁）
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Optional

import config
from core import llm_client
from core.cache_manager import get_parse_cache
from core.errors import (ErrorCategory, ErrorContext, ErrorSeverity,
                        record_error)
from core.json_utils import extract_json

MAP_CONCURRENCY = config.MAP_CONCURRENCY
BATCH_SIZE = 10
REDUCE_BATCH_SIZE = 8

# 使用并发安全的缓存管理器
cache_manager = get_parse_cache()

MAP_PROMPT = """你是需求文档分析专家。请从以下需求文档的多个片段中抽取结构化信息。__MAP__

只输出 JSON，格式：
{
  "objects": [{"name": "业务对象名", "attrs": ["属性1", "属性2"], "desc": "一句话说明"}],
  "rules": [{"name": "规则名", "desc": "规则内容"}],
  "flows": [{"name": "流程名", "steps": ["步骤1", "步骤2"]}]
}
片段中没有的项给空数组，不要编造。

文档片段：
{text}
"""

REDUCE_PROMPT = """你是需求文档分析专家。以下是从同一份需求文档多个片段中分别抽取的结构化信息（JSON 数组）。
请合并去重：同一业务对象合并属性；语义相同的规则/流程合并；保持信息完整。

只输出 JSON，格式：
{
  "objects": [{"name": "...", "attrs": ["..."], "desc": "..."}],
  "rules": [{"name": "...", "desc": "..."}],
  "flows": [{"name": "...", "steps": ["..."]}]
}

待合并的抽取结果：
{parts}
"""


@dataclass
class ExtractionResult:
    """抽取结果：包含数据和元信息"""
    data: dict
    source: str
    error: Optional[Exception] = None
    fallback_reason: Optional[str] = None


def _merge_lists(merged: dict, part: dict):
    """简单兜底合并：按 name 去重。"""
    for key in ("objects", "rules", "flows"):
        seen = {o.get("name") for o in merged.get(key, [])}
        for o in part.get(key, []):
            if o.get("name") and o["name"] not in seen:
                merged.setdefault(key, []).append(o)
                seen.add(o["name"])


def _local_merge(parts: list[dict]) -> dict:
    """本地兜底合并（不调用 LLM）：按名称去重合并。"""
    merged: dict = {"objects": [], "rules": [], "flows": []}
    for p in parts:
        _merge_lists(merged, p)
    return merged


def _llm_reduce(
    batch: list[dict],
    progress_cb: Optional[Callable] = None,
    tag: str = "",
    project_id: Optional[int] = None
) -> ExtractionResult:
    """调用 LLM 合并一批抽取结果；失败时降级到本地合并（显式标记）"""
    context = ErrorContext(project_id=project_id, batch_id=hash(tag))

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
            return ExtractionResult(data=merged, source="llm")
        else:
            reason = "LLM返回了非法JSON"
            record_error(
                category=ErrorCategory.LLM_CALL,
                severity=ErrorSeverity.WARNING,
                message=f"reduce{tag} LLM返回无效JSON，降级到本地合并",
                context=context,
                fallback_used=True,
                fallback_reason=reason
            )
            return ExtractionResult(
                data=_local_merge(batch),
                source="local_fallback",
                fallback_reason=reason
            )

    except Exception as e:
        reason = f"LLM调用异常: {type(e).__name__}"
        record_error(
            category=ErrorCategory.LLM_CALL,
            severity=ErrorSeverity.WARNING,
            message=f"reduce{tag} LLM调用失败，降级到本地合并: {e}",
            context=context,
            exception=e,
            fallback_used=True,
            fallback_reason=reason
        )
        return ExtractionResult(
            data=_local_merge(batch),
            source="local_fallback",
            error=e,
            fallback_reason=reason
        )


def _extract_batch(batch: list[dict], project_id: Optional[int] = None) -> ExtractionResult:
    """抽取一批分块（使用并发安全的缓存管理器）"""
    context = ErrorContext(project_id=project_id)

    parts = []
    for ch in batch:
        heading = f"标题：{ch['heading']}" if ch.get("heading") else "无标题"
        parts.append(f"--- 片段（{heading}）---\n{ch['content']}")
    text = "\n\n".join(parts)

    # 使用 CacheManager 读取缓存（自动处理损坏文件）
    cached_data = cache_manager.get(text)
    if cached_data:
        return ExtractionResult(data=cached_data, source="cache")

    prompt = MAP_PROMPT.replace("{text}", text)
    try:
        raw = llm_client.chat(
            [{"role": "user", "content": prompt}],
            role="extraction", response_json=True,
        )
        obj = extract_json(raw)

        if not obj:
            record_error(
                category=ErrorCategory.LLM_CALL,
                severity=ErrorSeverity.WARNING,
                message="LLM返回了无效的JSON",
                context=context,
                fallback_used=True,
                fallback_reason="返回空字典"
            )
            return ExtractionResult(
                data={},
                source="local_fallback",
                fallback_reason="LLM返回无效JSON"
            )

        # 使用 CacheManager 写入缓存（原子写入+文件锁）
        cache_manager.set(text, obj)

        return ExtractionResult(data=obj, source="llm")

    except Exception as e:
        record_error(
            category=ErrorCategory.LLM_CALL,
            severity=ErrorSeverity.ERROR,
            message=f"批次抽取失败: {e}",
            context=context,
            exception=e
        )
        return ExtractionResult(
            data={},
            source="local_fallback",
            error=e,
            fallback_reason=f"LLM调用异常: {type(e).__name__}"
        )


def extract_structured(
    chunks: list[dict],
    progress_cb: Optional[Callable] = None,
    reduce_cb: Optional[Callable] = None,
    project_id: Optional[int] = None
) -> dict:
    """chunks: [{"seq","heading","content"}] → {"objects","rules","flows"}。"""
    from core import cancel

    if not chunks:
        return {"objects": [], "rules": [], "flows": []}

    batches = [chunks[i:i + BATCH_SIZE] for i in range(0, len(chunks), BATCH_SIZE)]
    total = len(batches)
    results: list[ExtractionResult] = [None] * total
    done_count = 0
    cancelled = False

    fallback_count = 0

    with ThreadPoolExecutor(max_workers=MAP_CONCURRENCY) as pool:
        futures = {pool.submit(_extract_batch, b, project_id): i for i, b in enumerate(batches)}
        for fut in as_completed(futures):
            if project_id is not None and cancel.is_cancelled(project_id):
                cancelled = True
                for f in futures:
                    f.cancel()
                break

            idx = futures[fut]
            try:
                result = fut.result()
                results[idx] = result

                if result.source == "local_fallback":
                    fallback_count += 1

                if progress_cb:
                    cached = (result.source == "cache")
                    done_count += 1
                    progress_cb(done_count, total, cached)

            except Exception as e:
                record_error(
                    category=ErrorCategory.UNKNOWN,
                    severity=ErrorSeverity.ERROR,
                    message=f"批次{idx}执行异常: {e}",
                    context=ErrorContext(project_id=project_id, batch_id=idx),
                    exception=e
                )
                results[idx] = ExtractionResult(
                    data={},
                    source="local_fallback",
                    error=e,
                    fallback_reason="Future执行异常"
                )
                done_count += 1
                if progress_cb:
                    progress_cb(done_count, total, False)

    if cancelled:
        raise InterruptedError("用户取消了流水线")

    parts = [r.data for r in results if r and r.data]

    if not parts:
        record_error(
            category=ErrorCategory.LLM_CALL,
            severity=ErrorSeverity.ERROR,
            message="所有批次抽取均失败",
            context=ErrorContext(project_id=project_id)
        )
        return {"objects": [], "rules": [], "flows": []}

    if len(parts) == 1:
        return parts[0]

    level = 0
    while len(parts) > REDUCE_BATCH_SIZE:
        level += 1
        nxt = []
        for i in range(0, len(parts), REDUCE_BATCH_SIZE):
            batch = parts[i:i + REDUCE_BATCH_SIZE]
            if len(batch) == 1:
                nxt.append(batch[0])
            else:
                result = _llm_reduce(batch, reduce_cb, tag=f" L{level}-{i // REDUCE_BATCH_SIZE + 1}", project_id=project_id)
                nxt.append(result.data)
                if result.source == "local_fallback":
                    fallback_count += 1
        parts = nxt

    final_result = _llm_reduce(parts, reduce_cb, tag=" 最终", project_id=project_id)
    if final_result.source == "local_fallback":
        fallback_count += 1

    if fallback_count > 0:
        record_error(
            category=ErrorCategory.LLM_CALL,
            severity=ErrorSeverity.INFO,
            message=f"抽取完成，{fallback_count} 个批次使用了本地降级策略",
            context=ErrorContext(project_id=project_id),
            fallback_used=True,
            fallback_reason=f"共{fallback_count}次降级"
        )

    return final_result.data


def render_markdown(structured: dict, doc_names: list[str]) -> str:
    """把结构化数据渲染为《结构化原始数据》Markdown。"""
    lines = ["# 结构化原始数据", ""]
    if doc_names:
        lines += ["> 来源文档：" + "、".join(doc_names), ""]

    lines += ["## 一、业务对象", "", "| 对象 | 属性 | 说明 |", "|---|---|---|"]
    for o in structured.get("objects", []):
        attrs = "、".join(o.get("attrs", [])) or "-"
        lines.append(f"| {o.get('name','-')} | {attrs} | {o.get('desc','-')} |")

    lines += ["", "## 二、业务规则", "", "| 规则 | 内容 |", "|---|---|"]
    for r in structured.get("rules", []):
        lines.append(f"| {r.get('name','-')} | {r.get('desc','-')} |")

    lines += ["", "## 三、业务流程", ""]
    for f in structured.get("flows", []):
        steps = " → ".join(f.get("steps", []))
        lines.append(f"- **{f.get('name','-')}**：{steps}")

    lines += ["", "```json", json.dumps(structured, ensure_ascii=False, indent=2), "```"]
    return "\n".join(lines)
