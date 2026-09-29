"""把 :class:`~gametrans.web.app.WebApp` 接到 HTTP 上。

只用标准库 ``http.server``：面板是本机开发工具，不值得为它引入 Web 框架，
"内核零第三方依赖"这条优先级在这里同样适用。
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from gametrans.errors import WebError
from gametrans.web.app import WebApp

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def make_handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    """造一个把请求转给 ``app`` 的处理器类。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "gametrans"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
            self._dispatch("GET")

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch("HEAD")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            # 读请求体：既避免 HTTP/1.1 keep-alive 下的连接错位，也交给 app 处理
            # （面板唯一的写口要用它，见 /api/engine-option）
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""

            parsed = urlsplit(self.path)
            response = app.route(
                method, unquote(parsed.path), parse_qs(parsed.query), body
            )

            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(response.body)

        def log_message(self, format: str, *args: Any) -> None:
            """默认实现会往 stderr 刷每一条请求；面板在轮询，必须静音。"""

    return Handler


def create_server(
    app: WebApp, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> ThreadingHTTPServer:
    """建好服务器但**不**开始服务。测试用 ``port=0`` 拿随机端口。"""
    try:
        return ThreadingHTTPServer((host, port), make_handler(app))
    except OSError as exc:
        raise WebError(
            f"无法在 {host}:{port} 启动面板：{exc}",
            hint="端口可能被占用了，换一个：`gametrans web --port 8899`。",
        ) from None


def serve(
    app: WebApp,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    stream: Any = None,
) -> None:
    """前台阻塞地提供服务，直到 Ctrl+C。"""
    sink = stream if stream is not None else sys.stderr
    httpd = create_server(app, host, port)
    actual_host, actual_port = httpd.server_address[0], httpd.server_address[1]
    print(f"gametrans 面板已启动：http://{actual_host}:{actual_port}", file=sink)
    print(
        f"项目：{app.project_root}　｜　可写：引擎设置 / 配置 / 凭证 / 内容资源",
        file=sink,
    )
    print("执行类操作（scan / translate / writeback / pack）请用 CLI 或 MCP。", file=sink)
    print("按 Ctrl+C 停止。", file=sink)
    sink.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n面板已停止。", file=sink)
    finally:
        httpd.server_close()
