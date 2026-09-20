"""OpenAI 兼容协议 LLM 客户端（httpx 流式 SSE），支持按角色配置、离线 mock 模式与自动重试。

为什么必须流式：非流式（stream=False）时，服务端要生成完整份长文档（如 SRS）
才返回第一个字节，真实生成时间常超过读超时，httpx 必然 ReadTimeout；
流式调用下 token 持续到达，读超时只作用于相邻数据块的间隔，长生成不会被掐断。
"""
import json
import os
import time

import httpx

from config import get_llm_config, llm_mock_enabled
from core.mock_llm import mock_complete
from core import stream_bus

# LLM 调用是否走系统代理（HTTPS_PROXY 等环境变量控制）。
# 默认【不走】：LLM 端点通常是直连可达的（如国内阿里云/DeepSeek），若跟随系统
# 代理，一旦代理软件（梯子/Clash 等）关闭就会报 WinError 10061 连接被拒绝；
# 确需让 LLM 走代理的环境（如经代理访问 OpenAI）可在 .env 设 LLM_TRUST_PROXY=1。
LLM_TRUST_PROXY = os.getenv("LLM_TRUST_PROXY", "0") == "1"

MAX_RETRIES = 2            # 首次失败后最多再重试次数
RETRY_BACKOFF = 5          # 重试退避秒数（按 2 的指数递增）
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
CONNECT_TIMEOUT = 30       # 连接超时
WRITE_TIMEOUT = 60         # 请求体发送超时

# mock 模式下模拟的「思考」文本：真实推理模型会先发 reasoning_content，
# 前端要能在思考期就显示点东西，这条链路得能离线验证。
MOCK_THINKING = ("先梳理输入里的业务对象与约束，再按模板组织章节；"
                 "重点检查编号是否连续、每条需求是否可验证。")


class LLMError(Exception):
    pass


def _iter_sse_pieces(resp):
    """逐行解析 SSE 流，yield (kind, 增量文本)，kind 为 content 或 reasoning。

    推理模型（如 qwen3.8-max）先长时间只发 reasoning_content 再发 content，
    长文档这段思考可达数分钟。只取 content 的话，前端在整个思考期都是「0 字」，
    看着像卡死，所以两种增量都要往上报。"""
    for line in resp.iter_lines():
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        think = delta.get("reasoning_content")
        if think:
            yield ("reasoning", think)
        piece = delta.get("content")
        if piece:
            yield ("content", piece)
        elif not delta:
            # 少数实现不用 delta 而直接在 message 里返回
            msg = choices[0].get("message") or {}
            if msg.get("content"):
                yield ("content", msg["content"])


def _iter_sse_text(resp):
    """只取正文增量（交互对话面板用，思考过程不外显）。"""
    for kind, text in _iter_sse_pieces(resp):
        if kind == "content":
            yield text


def chat(messages: list[dict], role: str = "default",
         response_json: bool = False, temperature: float | None = None,
         progress_cb=None) -> str:
    """调用 chat/completions（流式），返回完整文本。role 决定使用哪套配置。
    对超时/网络错误/429/5xx 自动重试；4xx（鉴权、请求非法）立即抛错。
    progress_cb(total_chars)：每收到新增量时回调累计字符数（可为 None）。"""
    if llm_mock_enabled():
        return _mock_stream(messages, role)

    cfg = get_llm_config(role)
    if not cfg["api_key"] or cfg["api_key"].startswith("sk-your"):
        raise LLMError(
            "未配置有效的 LLM_API_KEY。请在 .env 中填写真实 key，"
            "或设置 LLM_MOCK=1 使用离线演示模式。"
        )

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg["temperature"] if temperature is None else temperature,
        "stream": True,
    }
    if response_json:
        payload["response_format"] = {"type": "json_object"}

    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    # read 超时作用于相邻数据块间隔：只要 token 持续到达就不会触发
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=cfg["timeout"],
                            write=WRITE_TIMEOUT, pool=CONNECT_TIMEOUT)

    last_err: LLMError | None = None
    for attempt in range(MAX_RETRIES + 1):
        # 每次尝试都从头生成：通知流式总线作废上一轮已推给前端的半截文本
        stream_bus.reset_attempt()
        try:
            with httpx.Client(timeout=timeout, trust_env=LLM_TRUST_PROXY) as client:
                with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code in RETRYABLE_STATUS:
                        body = resp.read().decode("utf-8", "ignore")[:200]
                        last_err = LLMError(f"LLM 服务返回 {resp.status_code}: {body}")
                    elif resp.status_code >= 400:
                        body = resp.read().decode("utf-8", "ignore")[:500]
                        raise LLMError(
                            f"LLM 服务返回错误 {resp.status_code}: {body}")
                    else:
                        parts = []
                        total = 0
                        for kind, piece in _iter_sse_pieces(resp):
                            if kind == "reasoning":
                                stream_bus.publish_thinking(piece)
                                continue
                            parts.append(piece)
                            total += len(piece)
                            stream_bus.publish_delta(piece)
                            if progress_cb:
                                progress_cb(total)
                        text = "".join(parts)
                        if text.strip():
                            return text
                        last_err = LLMError("LLM 返回内容为空")
        except LLMError:
            raise
        except httpx.RequestError as e:
            # 超时、连接失败等网络错误，可重试
            last_err = LLMError(f"LLM 调用失败: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * (2 ** attempt))

    raise last_err or LLMError("LLM 调用失败")


def _mock_stream(messages: list[dict], role: str) -> str:
    """离线 mock 也走流式推送：先推一小段思考再按小块喂正文，
    便于不烧 token 就能验证实时渲染链路（含推理模型思考期的前端表现）。"""
    text = mock_complete(messages, role)
    for i in range(0, len(MOCK_THINKING), 16):
        stream_bus.publish_thinking(MOCK_THINKING[i:i + 16])
        time.sleep(0.004)
    for i in range(0, len(text), 24):
        stream_bus.publish_delta(text[i:i + 24])
        time.sleep(0.005)
    return text


def chat_stream(messages: list[dict], role: str = "default",
                temperature: float | None = None):
    """生成器版本：逐块 yield 增量文本（供 SSE 透传给浏览器）。
    交互对话场景不做自动重试——失败立即抛 LLMError 让调用方提示用户。"""
    if llm_mock_enabled():
        text = mock_complete(messages, role)
        # 模拟流式：按小块吐出，便于前端呈现打字效果
        for i in range(0, len(text), 12):
            yield text[i:i + 12]
            time.sleep(0.015)
        return

    cfg = get_llm_config(role)
    if not cfg["api_key"] or cfg["api_key"].startswith("sk-your"):
        raise LLMError(
            "未配置有效的 LLM_API_KEY。请在 .env 中填写真实 key，"
            "或设置 LLM_MOCK=1 使用离线演示模式。"
        )

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg["temperature"] if temperature is None else temperature,
        "stream": True,
    }
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=cfg["timeout"],
                            write=WRITE_TIMEOUT, pool=CONNECT_TIMEOUT)

    with httpx.Client(timeout=timeout, trust_env=LLM_TRUST_PROXY) as client:
        with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode("utf-8", "ignore")[:500]
                raise LLMError(f"LLM 服务返回错误 {resp.status_code}: {body}")
            got_any = False
            for piece in _iter_sse_text(resp):
                got_any = True
                yield piece
            if not got_any:
                raise LLMError("LLM 返回内容为空")
