"""本地只读 WebUI 面板。

它是**交互层的具体形态**，不是 agent 的私有通道：渲染的是同一套视图与可见性策略，
默认只显示用户可见的信息；对用户透明的那些要点开开关才看得到。

零依赖：``http.server`` + 静态文件，没有前端构建步骤。

写路径（在浏览器里触发 scan / translate / writeback）**已预留但未实现** ——
``POST /api/operations/<name>`` 端点存在，写操作一律返回 501 并说明该走 CLI 还是 MCP。
"""

from __future__ import annotations

from gametrans.web.app import WebApp, Response
from gametrans.web.server import create_server, serve

__all__ = ["Response", "WebApp", "create_server", "serve"]
