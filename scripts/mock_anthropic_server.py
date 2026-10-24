#!/usr/bin/env python3
"""离线 E2E 测试用的本地 Anthropic 兼容 mock 服务器。

不访问外网，按请求特征返回确定性的 SSE 流式响应，用于在无 API Key /
无网络环境下对 kyle-core 做端到端系统测试：

- system 含 "summarizes conversations"（compact 调用）→ 返回固定摘要文本
- 最后一条 user 消息是 tool_result（工具已执行）→ 返回 "TASK DONE" 并 end_turn
- 否则（首次调用）→ 返回 thinking block + bash tool_use（echo kyle-e2e）

用法：python scripts/mock_anthropic_server.py [port]   # 默认 8790
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _sse(handler: BaseHTTPRequestHandler, events: list[tuple[str, dict]]) -> None:
    # 将 (event, data) 列表按 Anthropic SSE 格式写出并逐块刷新
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    for event, data in events:
        chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        handler.wfile.write(chunk.encode("utf-8"))
        handler.wfile.flush()


class Handler(BaseHTTPRequestHandler):
    # 关闭默认请求日志，保持测试输出干净
    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        if not self.path.endswith("/v1/messages"):
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        system_text = json.dumps(body.get("system", ""), ensure_ascii=False)
        messages = body.get("messages", [])
        last = messages[-1] if messages else {}
        last_content = last.get("content", "")
        saw_tool_result = (
            last.get("role") == "user"
            and isinstance(last_content, list)
            and any(b.get("type") == "tool_result" for b in last_content)
        )

        if "summarizes conversations" in system_text:
            self._respond_text("MOCK COMPACT SUMMARY: goal done, files verified.")
        elif saw_tool_result:
            self._respond_text("TASK DONE")
        else:
            self._respond_tool_use()

    # 返回 thinking + text + tool_use 的完整流，覆盖 provider 的三种 block 解析
    def _respond_tool_use(self) -> None:
        events: list[tuple[str, dict]] = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_mock_1", "type": "message", "role": "assistant",
                "content": [], "model": "mock", "stop_reason": None,
                "usage": {"input_tokens": 100, "output_tokens": 1},
            }}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "plan: run bash echo"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("content_block_start", {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "toolu_mock_1",
                               "name": "bash", "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta",
                       "partial_json": "{\"command\": \"echo kyle-e2e\"}"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            ("message_delta", {"type": "message_delta",
             "delta": {"stop_reason": "tool_use", "stop_sequence": None},
             "usage": {"output_tokens": 20}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        _sse(self, events)

    # 返回纯文本 end_turn 响应
    def _respond_text(self, text: str) -> None:
        events: list[tuple[str, dict]] = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_mock_2", "type": "message", "role": "assistant",
                "content": [], "model": "mock", "stop_reason": None,
                "usage": {"input_tokens": 100, "output_tokens": 1},
            }}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": text}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
             "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 10}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        _sse(self, events)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock anthropic server listening on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
