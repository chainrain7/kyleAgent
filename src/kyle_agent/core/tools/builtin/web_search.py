"""web_search 工具(改造 D)。

设计要点:
- 搜索是"只读但外发"的操作——不改动本地文件,但会把任务内容发往外部服务,
  因此权限默认策略设为 ASK(与 bash 同级),这是本工具最有讲头的取舍;
- 后端可插拔:默认 DuckDuckGo HTML 端点(免 key),可用环境变量切换到
  博查/Tavily 等 JSON API;
- 结果做条数与长度双截断,防止长结果撑爆上下文(tool_result 是长尾大头)。
"""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote_plus

import httpx
from pydantic import BaseModel, ConfigDict, Field

from kyle_agent.core.tools.base import BaseTool, ToolResult

_MAX_RESULTS = 5
_SNIPPET_MAX = 300
_TIMEOUT_S = 15.0
_DDG_HTML = "https://html.duckduckgo.com/html/"

_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
)
_SNIPPET_RE = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")


class WebSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str = Field(min_length=1, max_length=400)
    max_results: int = Field(default=_MAX_RESULTS, ge=1, le=10)


class WebSearchTool(BaseTool):
    params_model = WebSearchParams
    name = "web_search"
    description = (
        "Search the web and return top results (title, url, snippet). "
        "Useful for current information beyond your training data. "
        "Read-only but sends the query to an external search service."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {
                "type": "integer",
                "description": f"Max results to return (default {_MAX_RESULTS}, max 10).",
            },
        },
        "required": ["query"],
    }

    # 注入 http 客户端便于测试;不注入则按环境变量决定后端
    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client

    # 执行搜索:JSON API 后端(配置了 key)优先,否则 DuckDuckGo HTML
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = WebSearchParams.model_validate(params)
        api_key = os.environ.get("KYLE_SEARCH_API_KEY", "")
        endpoint = os.environ.get("KYLE_SEARCH_ENDPOINT", "")
        try:
            if api_key and endpoint:
                results = await self._search_json_api(
                    endpoint, api_key, p.query, p.max_results
                )
            else:
                results = await self._search_ddg(p.query, p.max_results)
        except Exception as exc:
            return ToolResult(
                content=f"web_search failed: {exc}", is_error=True, error_type="runtime_error"
            )
        if not results:
            return ToolResult(content="No results found.")
        lines = [
            f"{i + 1}. {title}\n   {url}\n   {snippet}"
            for i, (title, url, snippet) in enumerate(results)
        ]
        return ToolResult(content="\n\n".join(lines))

    # 调用 JSON API 后端(博查/Tavily 风格:POST {query, count} → results[])
    async def _search_json_api(
        self, endpoint: str, api_key: str, query: str, max_results: int
    ) -> list[tuple[str, str, str]]:
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT_S)
        try:
            resp = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"query": query, "count": max_results},
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            out: list[tuple[str, str, str]] = []
            for item in (data.get("results") or data.get("data") or [])[:max_results]:
                out.append((
                    str(item.get("title", "")).strip()[:120],
                    str(item.get("url", item.get("link", ""))).strip(),
                    str(item.get("snippet", item.get("content", ""))).strip()[:_SNIPPET_MAX],
                ))
            return out
        finally:
            if self._client is None:
                await client.aclose()

    # 调用 DuckDuckGo HTML 端点并解析结果(免 key 的默认后端)
    async def _search_ddg(
        self, query: str, max_results: int
    ) -> list[tuple[str, str, str]]:
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT_S)
        try:
            resp = await client.get(_DDG_HTML, params={"q": query})
            resp.raise_for_status()
            return _parse_ddg_html(resp.text, max_results, query)
        finally:
            if self._client is None:
                await client.aclose()


# 从 DuckDuckGo HTML 页面解析标题/链接/摘要
def _parse_ddg_html(
    html: str, max_results: int, query: str
) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    titles = _RESULT_RE.findall(html)
    snippets = _SNIPPET_RE.findall(html)
    for i, (href, raw_title) in enumerate(titles[:max_results]):
        title = _TAG_RE.sub("", raw_title).strip()
        snippet = (
            _TAG_RE.sub("", snippets[i]).strip()[:_SNIPPET_MAX]
            if i < len(snippets)
            else ""
        )
        url = _unwrap_ddg_href(href)
        if title and url:
            results.append((title, url, snippet))
    if not results:
        # 解析失败时给出可诊断的空结果而不是硬错误
        _ = quote_plus(query)
    return results


# DuckDuckGo 的跳转链接形如 //duckduckgo.com/l/?uddg=<encoded>,还原真实 URL
def _unwrap_ddg_href(href: str) -> str:
    if "uddg=" in href:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if qs.get("uddg"):
            return qs["uddg"][0]
    if href.startswith("//"):
        return "https:" + href
    return href
