"""MCP 控制面 —— 手写的 stdio JSON-RPC 服务端。

不引入任何第三方 MCP SDK：协议本身很小（initialize / tools/list / tools/call），
而"内核零依赖"是 spec 里排在前面的优先级。

工具清单直接来自 :mod:`gametrans.operations`，所以 CLI 有什么，agent 就能调什么。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from gametrans import __version__
from gametrans.operations import (
    Context,
    arguments_for,
    error_envelope,
    get_operation_by_tool,
    json_dumps,
    result_envelope,
    tool_descriptor,
    all_operations,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "gametrans"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class MCPServer:
    """无状态的 JSON-RPC 处理器。``handle`` 收一条消息，返回一条响应（通知返回 None）。"""

    def __init__(self, *, server_name: str = SERVER_NAME, version: str = __version__) -> None:
        self.server_name = server_name
        self.version = version

    # ---- 响应构造 -----------------------------------------------------------

    @staticmethod
    def _result(msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _tool_text(payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": json_dumps(payload)}],
            "isError": is_error,
        }

    # ---- 主循环 -------------------------------------------------------------

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return self._error(None, INVALID_REQUEST, "请求必须是 JSON 对象")

        msg_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return self._error(msg_id, INVALID_REQUEST, "缺少 method 字段")

        # 通知没有 id，不产生响应
        if "id" not in message:
            return None

        if method == "initialize":
            return self._result(msg_id, self._initialize_result())
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": [tool_descriptor(op) for op in all_operations()]})
        if method == "tools/call":
            params = message.get("params") or {}
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return self._error(msg_id, INVALID_PARAMS, "tools/call 需要 params.name")
            return self._result(
                msg_id, self._call_tool(params["name"], params.get("arguments") or {})
            )
        return self._error(msg_id, METHOD_NOT_FOUND, f"未实现的方法：{method}")

    def _initialize_result(self) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.server_name, "version": self.version},
        }

    # ---- 工具调用 -----------------------------------------------------------

    def _call_tool(self, tool_name: str, arguments: Any) -> dict[str, Any]:
        operation = get_operation_by_tool(tool_name)
        if operation is None:
            return self._tool_text(
                {
                    "ok": False,
                    "error": {
                        "type": "UnknownTool",
                        "message": f"未知工具：{tool_name}",
                        "hint": "用 tools/list 查看可用工具。",
                    },
                },
                is_error=True,
            )

        if not isinstance(arguments, dict):
            return self._tool_text(
                {
                    "ok": False,
                    "error": {
                        "type": "InvalidArguments",
                        "message": "arguments 必须是对象",
                        "hint": None,
                    },
                },
                is_error=True,
            )

        raw_workdir = arguments.get("workdir")
        ctx = Context(
            project_root=Path(str(arguments.get("project") or ".")).expanduser(),
            workdir=Path(str(raw_workdir)).expanduser() if raw_workdir else None,
            json_mode=True,
        )

        try:
            data = operation.handler(ctx, arguments_for(operation, arguments))
        except Exception as exc:  # noqa: BLE001 - 工具失败要变成工具结果，而不是协议错误
            return self._tool_text(error_envelope(exc), is_error=True)
        return self._tool_text(result_envelope(data), is_error=False)


def serve(stdin=None, stdout=None) -> None:
    """stdio 传输：一行一条 JSON-RPC 消息。"""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    server = MCPServer()

    for line in source:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError as exc:
            response = MCPServer._error(None, PARSE_ERROR, f"JSON 解析失败：{exc.msg}")
        else:
            response = server.handle(message)
        if response is None:
            continue
        sink.write(json.dumps(response, ensure_ascii=False) + "\n")
        sink.flush()
