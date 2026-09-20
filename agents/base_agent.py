"""智能体基类：统一的『Markdown + 末尾 ```json 元数据』输出、pydantic 校验、失败重试。"""
from typing import Callable

from core import llm_client
from core.json_utils import split_markdown_and_meta

MAX_RETRY = 2


class AgentOutputError(Exception):
    pass


def run_agent(role: str, system_prompt: str, user_prompt: str,
              validator: Callable[[dict], str | None] | None = None,
              soft_validator: Callable[[dict], str | None] | None = None,
              expect_json_only: bool = False,
              progress_cb: Callable[[int], None] | None = None) -> tuple[str, dict]:
    """调用 LLM 并校验输出。

    - expect_json_only=False：输出为 Markdown 文档 + 末尾 ```json 元数据块，
      返回 (markdown, meta)。
    - expect_json_only=True：输出为纯 JSON（如测试用例），返回 (markdown="", meta)。
    validator(meta) 返回错误信息字符串表示校验失败，None 表示通过。
    soft_validator(meta) 用于「追溯完整性」这类应当强制、但不值得为它丢弃整份产物的
    指标：未通过时同样回灌错误让模型修正，但最后一次仍不通过就接受输出，
    并把问题写进 meta["_warnings"]，交人工评审裁决。
    progress_cb(chars)：流式生成进度回调（累计字符数），可为 None。
    校验失败会把错误信息回灌给 LLM 重试，最多 MAX_RETRY 次。
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_err = None
    for attempt in range(MAX_RETRY + 1):
        if attempt > 0 and last_err:
            messages.append({"role": "assistant", "content": last_raw})
            messages.append({"role": "user",
                             "content": f"上次输出校验失败：{last_err}\n请修正后重新完整输出。"})
        raw = llm_client.chat(messages, role=role, response_json=expect_json_only,
                              progress_cb=progress_cb)
        last_raw = raw

        if expect_json_only:
            from core.json_utils import extract_json
            meta = extract_json(raw)
            markdown = ""
        else:
            markdown, meta = split_markdown_and_meta(raw)

        if meta is None:
            last_err = "输出中缺少合法的 JSON 元数据块"
            continue
        if validator:
            err = validator(meta)
            if err:
                last_err = err
                continue
        if soft_validator:
            err = soft_validator(meta)
            if err:
                last_err = err
                if attempt == MAX_RETRY:
                    meta.setdefault("_warnings", []).append(err)
                    return markdown, meta
                continue
        return markdown, meta

    raise AgentOutputError(f"智能体输出连续 {MAX_RETRY + 1} 次校验失败，最后错误：{last_err}")
