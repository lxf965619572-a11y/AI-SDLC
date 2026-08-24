"""联网检索（方案 B：搜索 API 手动检索，非模型自主决定）。

三层可控开关：
1. 配置级总开关：未配置 WEB_SEARCH_API_KEY 时整体关闭（前端连开关都不显示）；
2. 单次开关：前端「🌐 联网」按钮逐条消息控制（默认关）；
3. 失败兜底：任何超时/网络/解析错误都返回空列表，绝不阻断对话。

支持提供商（.env 的 WEB_SEARCH_PROVIDER 切换）：
- bocha（默认，博查 AI 搜索，国内可用）
- tavily（面向 AI 的搜索 API）
- bing（Bing Web Search v7）

内网环境不配置 key 即可，本模块不会产生任何外部请求。
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT = float(os.getenv("WEB_SEARCH_TIMEOUT", "8"))
MAX_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
SNIPPET_MAX = 300  # 单条摘要进入上下文的最大字符数

_PROVIDERS = ("bocha", "tavily", "bing")


def provider() -> str:
    p = os.getenv("WEB_SEARCH_PROVIDER", "bocha").strip().lower()
    return p if p in _PROVIDERS else "bocha"


def enabled() -> bool:
    """配置级总开关：配了非占位 key 才启用。"""
    key = os.getenv("WEB_SEARCH_API_KEY", "").strip()
    return bool(key) and not key.startswith("sk-your")


def search(query: str, count: int | None = None) -> list[dict]:
    """联网搜索，返回 [{"title","url","snippet"}]。
    永不抛异常：失败记日志并返回空列表。"""
    if not enabled() or not query.strip():
        return []
    count = count or MAX_RESULTS
    key = os.getenv("WEB_SEARCH_API_KEY", "").strip()
    try:
        fn = {"bocha": _search_bocha, "tavily": _search_tavily,
              "bing": _search_bing}[provider()]
        results = fn(query, count, key)
        return results[:count]
    except Exception as e:
        logger.warning("联网搜索失败（已忽略，继续基于项目上下文回答）: %s", e)
        return []


_NEWS_HINTS = ("今天", "今日", "最新", "新闻", "近期", "最近", "本周", "热点", "动态")


def _news_intent(query: str) -> bool:
    """简单意图判断：问新闻/时效性问题时收紧时间范围，否则常规检索。"""
    return any(k in query for k in _NEWS_HINTS)


def _search_bocha(query: str, count: int, key: str) -> list[dict]:
    # 新闻类查询限定近一周，常规查询不限时间
    freshness = "oneWeek" if _news_intent(query) else "noLimit"
    resp = httpx.post(
        "https://api.bochaai.com/v1/web-search",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json={"query": query, "freshness": freshness,
              "count": count, "summary": True},
        timeout=SEARCH_TIMEOUT)
    resp.raise_for_status()
    pages = (resp.json().get("data") or {}).get("webPages") or {}
    return _normalize(pages.get("value") or [],
                      title_key="name", url_key="url", snippet_key="snippet")


def _search_tavily(query: str, count: int, key: str) -> list[dict]:
    payload = {"api_key": key, "query": query,
               "topic": "news" if _news_intent(query) else "general",
               "max_results": count, "search_depth": "basic"}
    if _news_intent(query):
        payload["days"] = 7  # 新闻类查询限定近 7 天，提高时效性
    resp = httpx.post("https://api.tavily.com/search",
                      json=payload, timeout=SEARCH_TIMEOUT)
    resp.raise_for_status()
    return _normalize(resp.json().get("results") or [],
                      title_key="title", url_key="url", snippet_key="content")


def _search_bing(query: str, count: int, key: str) -> list[dict]:
    resp = httpx.get(
        "https://api.bing.microsoft.com/v7.0/search",
        params={"q": query, "count": count, "mkt": "zh-CN"},
        headers={"Ocp-Apim-Subscription-Key": key},
        timeout=SEARCH_TIMEOUT)
    resp.raise_for_status()
    pages = resp.json().get("webPages") or {}
    return _normalize(pages.get("value") or [],
                      title_key="name", url_key="url", snippet_key="snippet")


def _normalize(items: list[dict], title_key: str, url_key: str,
               snippet_key: str) -> list[dict]:
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = (it.get(title_key) or "").strip()
        url = (it.get(url_key) or "").strip()
        snippet = (it.get(snippet_key) or "").strip()
        if not (title or snippet):
            continue
        out.append({"title": title or url, "url": url,
                    "snippet": snippet[:SNIPPET_MAX]})
    return out


def format_as_context(results: list[dict]) -> str:
    """把搜索结果格式化为注入 system prompt 的参考块。"""
    if not results:
        return ""
    lines = ["\n\n【网络搜索结果】以下为互联网检索结果（含发布日期/时间），仅供补充参考；"
             "可能与项目文档冲突或已过时，冲突时以项目产物为准。"
             "回答网络相关问题时请充分利用这些结果，按相关性与日期组织回答并注明信息来源；"
             "结果中没有的内容不要编造，但已有结果中的信息应直接、完整地呈现给用户："]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}"
                     + (f"（{r['url']}）" if r["url"] else ""))
        if r["snippet"]:
            lines.append(f"   摘要：{r['snippet']}")
    return "\n".join(lines)
