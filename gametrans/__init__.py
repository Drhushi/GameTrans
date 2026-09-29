"""gametrans —— AI 驱动的游戏翻译工作台（雏形）。

架构分层::

    提取层 extract   ── 从游戏文件抽出全部待译内容，整理成「带权路径图」
    翻译层 translate ── 按路径图串行/并行翻译，产出译文并沉淀知识
    写回层 writeback ── 把译文写回游戏，制作并封包翻译补丁
    资源层 resource  ── 翻译资源（**术语书**：一行一个实体）+ 引擎资源（引擎支持包）
    交互层 interact  ── 软件直接向用户呈现信息与配置；agent 可改写其内容

两条并行的控制/呈现通道：

* **控制通道（agent）**  ``gametrans.cli`` 与 ``gametrans.mcp_server``。agent
  对五层拥有完整控制权，可以只跑一层、改参数再跑、中途介入。
* **呈现通道（用户）**  ``gametrans.layers.interaction``。软件绕过 agent 直接
  向用户投递信息，由可见性策略决定哪些信息用户关心、哪些对用户透明。

引擎隔离：内核（``gametrans.core`` / ``gametrans.layers`` / ``gametrans.providers``
/ ``gametrans.cli`` / ``gametrans.mcp_server``）不得 import 任何具体引擎实现。
具体引擎只在 ``gametrans.engines.<name>`` 里，通过 ``EngineSupportPack`` 契约接入。
``tests/test_isolation.py`` 用 AST 扫描硬性守住这条边界。
"""

from __future__ import annotations

__version__ = "0.2.1"
__all__ = ["__version__"]
