from __future__ import annotations

import httpx

from kyle_agent.core.permissions.policy import DEFAULT_POLICIES, PermissionDecision
from kyle_agent.core.tools.builtin.web_search import (
    WebSearchTool,
    _parse_ddg_html,
    _unwrap_ddg_href,
)

_DDG_HTML = (
    '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">'
    "Example Title</a>"
    '<a class="result__snippet">A <b>great</b> snippet</a>'
)


# 功能：DuckDuckGo HTML 解析出标题/链接/摘要,并解包跳转链接
# 设计：构造最小 HTML(带 uddg 跳转与内嵌标签),断言标签剥离与 URL 还原
def test_parse_ddg_html() -> None:
    results = _parse_ddg_html(_DDG_HTML, 5, "q")
    assert results == [("Example Title", "https://example.com/a", "A great snippet")]


# 功能：uddg 缺失时按协议相对链接处理,普通链接原样返回
# 设计：覆盖 _unwrap_ddg_href 的三个分支
def test_unwrap_ddg_href_variants() -> None:
    assert _unwrap_ddg_href("//example.com/x") == "https://example.com/x"
    assert _unwrap_ddg_href("https://example.com/y") == "https://example.com/y"


# 功能：web_search 默认权限策略为 ASK(只读但外发)
# 设计：权限语义是本工具的核心取舍——外发查询与 bash 同级默认询问
def test_web_search_default_policy_is_ask() -> None:
    assert DEFAULT_POLICIES["web_search"].default is PermissionDecision.ASK


# 功能：未配置 API key 时走 DuckDuckGo HTML 后端并返回格式化结果
# 设计：MockTransport 拦截 GET,返回合成 HTML;断言输出含序号/标题/URL
async def test_invoke_ddg_backend() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "html.duckduckgo.com" in str(request.url)
        assert request.url.params["q"] == "kyle agent"
        return httpx.Response(200, text=_DDG_HTML)

    tool = WebSearchTool(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await tool.invoke({"query": "kyle agent"})
    assert not result.is_error
    assert "Example Title" in result.content
    assert "https://example.com/a" in result.content


# 功能：配置 KYLE_SEARCH_API_KEY/ENDPOINT 后切换到 JSON API 后端
# 设计：MockTransport 拦截 POST,断言 Bearer 头、请求体与 results 解析
async def test_invoke_json_api_backend(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("KYLE_SEARCH_API_KEY", "sk-test")
    monkeypatch.setenv("KYLE_SEARCH_ENDPOINT", "https://search.test/api")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-test"
        import json

        body = json.loads(request.content)
        assert body == {"query": "kyle", "count": 3}
        return httpx.Response(200, json={"results": [{
            "title": "T1", "url": "https://t1.test", "snippet": "S1",
        }]})

    tool = WebSearchTool(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await tool.invoke({"query": "kyle", "max_results": 3})
    assert "T1" in result.content and "https://t1.test" in result.content


# 功能：后端 HTTP 错误返回 is_error 的 ToolResult 而非抛异常
# 设计：工具契约要求错误回填给模型继续决策;断言 error_type=runtime_error
async def test_invoke_backend_error_returns_error_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    tool = WebSearchTool(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await tool.invoke({"query": "x"})
    assert result.is_error
    assert result.error_type == "runtime_error"
    assert "web_search failed" in result.content
