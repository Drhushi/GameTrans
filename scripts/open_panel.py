"""gametrans 面板的"打开方式"：给小白用户的双击 / 拖拽入口，也是 agent 的开面板入口。

用法：

* 命令行：``python scripts/open_panel.py <游戏目录>``（agent 用这条，路径自己说了算）；
* 不带路径：**先用上一次那个项目**（记在本仓库的 ``.gametrans/panel.json``），
  不再每次都弹目录选择框；要换就带路径，或者显式 ``--pick`` 弹框；
* 双击仓库根目录的 ``打开面板.bat``（Windows）：等价于不带路径；
* 或者把游戏文件夹**拖到**那个 .bat 图标上。

它做四件事：

1. 目标游戏还没有工作区（``.gametrans/``）就自动初始化——引擎自动探测，
   目标语言默认 ``zh_CN``（面板的设置里随时能改）；
2. 挑一个**空闲**端口（多个游戏的面板可以同时开着，互不抢占）；
3. 后台起 ``gametrans web``；
4. 等 ``/api/ping`` 通了自动开面板：装了 pywebview（``pip install pywebview``）
   就开一个**独立桌面窗口**（无边框，标题栏由面板自绘；Windows 上用系统自带的
   Edge WebView2 渲染），关掉窗口即停止该项目的面板服务；没装或开不成则退回浏览器，
   关掉控制台窗口（或 Ctrl+C）停止服务。``--browser`` 强制走浏览器，``--no-browser`` 什么都不开。

只用标准库；pywebview 是可选的桌面壳，不装不影响任何功能。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PANEL_HOST = "127.0.0.1"

# 从任意位置（双击 / 拖拽 / 计划任务）调用都要能 import 到仓库里的 gametrans
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gametrans.userconfig import panel_state_file  # noqa: E402 - sys.path 先行

#: 上一次打开的项目记在这里 —— 不带路径时用它，免得每次都弹框让人挑。
#: 落点解析收口在内核（源码检出 = 检出根/.gametrans；冻结版见 panel_state_file）。
LAST_PROJECT_FILE = panel_state_file()

# pythonw（打开面板.pyw 的无控制台入口）下 stdout/stderr 是 None：print 会直接炸
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def pick_free_port() -> int:
    """向操作系统要一个当前空闲的端口——多个项目的面板并存时不撞车。"""
    with socket.socket() as sock:
        sock.bind((PANEL_HOST, 0))
        return int(sock.getsockname()[1])


def ensure_project(game: Path) -> None:
    """还没有工作区就初始化；已初始化的原样返回（幂等）。"""
    if (game / ".gametrans" / "project.json").is_file():
        return
    from gametrans.core.session import ProjectSession

    ProjectSession.init(game, target_language="zh_CN")
    print(f"已初始化工作区（引擎自动探测，目标语言 zh_CN）：{game}")


def wait_for_panel(port: int, *, timeout: float = 15.0) -> bool:
    """等服务真正应答再开浏览器，避免浏览器落到一个"连接被拒绝"的页面上。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://{PANEL_HOST}:{port}/api/ping", timeout=1
            ) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            time.sleep(0.3)
    return False


def pick_folder() -> str | None:
    """弹系统目录选择框（tkinter 是标准库）；没有图形环境就返回 None。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None
    root = tk.Tk()
    root.withdraw()
    try:
        return filedialog.askdirectory(title="选择游戏目录")
    finally:
        root.destroy()


def remembered_project() -> Path | None:
    """上一次打开的项目（文件不存在、内容坏了、目录没了，都当没记过）。"""
    try:
        data = json.loads(LAST_PROJECT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    path = Path(str(data.get("project") or "")).expanduser()
    return path if path.is_dir() else None


def remember_project(game: Path) -> None:
    """记下这次开的项目，挪到「最近打开」最前面（设置页一键切换的数据源）。

    写不进去不影响开面板，所以失败就算了。
    """
    try:
        from gametrans.web.app import merge_recent

        try:
            previous = json.loads(LAST_PROJECT_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        LAST_PROJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_PROJECT_FILE.write_text(
            json.dumps(merge_recent(previous, str(game)), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


class _ShellApi:
    """暴露给页面（``window.pywebview.api``）的窗口操作。

    窗口是无边框的：- ▢ ✕ 三颗按钮与缩放热区由前端自绘（static/js/titlebar.js），
    动作都落到这里 —— 页面只认 window.pywebview.api，不碰窗口系统。
    """

    MIN_W, MIN_H = 900, 600

    def __init__(self) -> None:
        import webview

        self._webview = webview
        self._maximized = False

    def _window(self):
        return self._webview.windows[0]

    def minimize(self) -> None:
        self._window().minimize()

    def toggle_maximize(self) -> None:
        window = self._window()
        if self._maximized:
            window.restore()
        else:
            window.maximize()
        self._maximized = not self._maximized

    def close(self) -> None:
        self._window().destroy()

    def set_title(self, project_name: str) -> None:
        """页面认出当前项目后同步窗口名（就地切项目时窗口名跟着换）。"""
        self._window().set_title(f"gametrans 面板 · {project_name}")

    def resize_by(self, dw: int, dh: int) -> None:
        """前端缩放热区调的增量缩放（无边框窗口没有系统边缘可拖）。"""
        window = self._window()
        width = max(self.MIN_W, window.width + dw)
        height = max(self.MIN_H, window.height + dh)
        if width != window.width or height != window.height:
            window.resize(width, height, fix_point="north-west")

    def pick_folder(self) -> str | None:
        """系统目录选择框（面板「切换项目 → 浏览…」用）；取消返回 None。"""
        result = self._window().create_file_dialog(self._webview.FOLDER_DIALOG)
        return result[0] if result else None


def open_in_window(url: str, title: str) -> bool:
    """有 pywebview 就开一个独立桌面窗口；开不成返回 False（调用方退回浏览器）。

    Windows / Linux 用无边框（frameless）+ 页面自绘标题栏，贴合面板自己的设计；
    macOS 刻意**不**无边框：保留系统标题栏，左上角那套原生红绿灯按钮照常工作，
    页面的自绘标题栏也会自行隐藏（js/titlebar.js 按 platform 跳过）。
    easy_drag 关掉——整窗乱拖会毁掉路径图的拖拽，只认页里的 pywebview-drag-region。
    ``webview.start()`` 会一直阻塞到用户关窗，正常返回即窗口已关闭。
    """
    try:
        import webview
    except ImportError:
        return False
    try:
        webview.create_window(
            title,
            url,
            width=1280,
            height=860,
            frameless=sys.platform != "darwin",
            easy_drag=False,
            js_api=_ShellApi(),
        )
        webview.start()
    except Exception as exc:
        # 退回浏览器前把原因说出来：桌面壳排错不能靠猜
        print(f"(桌面窗口开不成：{exc!r}，退回浏览器)")
        return False
    return True


def resolve_game(raw: list[str], *, force_pick: bool = False) -> Path | None:
    """命令行参数 → 记着的上一个 → 目录选择框。``raw`` 不含脚本名。"""
    game_arg = next((a for a in raw if not a.startswith("-")), "")
    if game_arg:
        return Path(game_arg).expanduser().resolve()
    if not force_pick:
        last = remembered_project()
        if last is not None:
            print(f"用上一次的项目（要换：把路径跟在命令后面，或加 --pick 弹框）：{last}")
            return last
    picked = pick_folder()
    return Path(picked).resolve() if picked else None


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # 桌面壳优先：PySide6 在就开多标签壳（主页选项目 + 每项目一个标签页）。
    # --pick / --browser 保持旧语义（弹系统目录框 / 走浏览器）；壳不可用则照旧退回下面的流程。
    if "--browser" not in raw and "--pick" not in raw:
        try:
            from desktop_shell import run as run_shell
        except Exception as exc:  # noqa: BLE001 - 壳缺什么都要如实说，然后退回
            print(f"(桌面壳不可用：{exc!r}，退回原方式)")
        else:
            positional = next((a for a in raw if not a.startswith("-")), "")
            code = run_shell(Path(positional).expanduser() if positional else None)
            if code != 2:  # 2 = 壳自身宣告不可用，落回下面的老路
                return code
    game = resolve_game(raw, force_pick="--pick" in raw)
    if game is None:
        print("没有选择游戏目录。")
        return 2
    if not game.is_dir():
        print(f"不是目录：{game}")
        return 2

    try:
        ensure_project(game)
    except Exception as exc:  # noqa: BLE001 - 给小白用户一句人话，而不是堆栈
        print(f"打不开这个项目：{getattr(exc, 'message', None) or exc}")
        return 1
    remember_project(game)

    port = pick_free_port()
    # 无控制台入口（pythonw / 打开面板.pyw）下子进程拿不到输出句柄，gametrans
    # 自己的 print 会直接炸 —— 指到工作区旁的日志文件，出事也有得查；
    # 有控制台时保持继承，日志照看。按解释器名字判定：sys.stdout 在模块导入时
    # 已被上面的兜底补过 devnull，靠它判就不准了。
    windowless = sys.executable.lower().endswith("pythonw.exe")
    server_log = None
    if windowless:
        server_log = open(LAST_PROJECT_FILE.parent / "panel-server.log", "wb")
    server = subprocess.Popen(
        [
            sys.executable, "-m", "gametrans", "web",
            "-p", str(game), "--port", str(port),
        ],
        cwd=str(REPO_ROOT),
        stdout=server_log if windowless else None,
        stderr=subprocess.STDOUT if windowless else None,
    )
    url = f"http://{PANEL_HOST}:{port}"
    print(f"面板启动中：{url}")
    print(f"项目：{game}")
    print("关掉面板窗口（或本控制台 / Ctrl+C）即停止这个项目的面板。")
    if not wait_for_panel(port):
        print("面板好像没能起来 —— 看看上面的报错，或把这段话发给懂的人。")
    elif os.environ.get("GT_NO_BROWSER") == "1" or "--no-browser" in raw:
        print("(跳过打开面板窗口)")
    elif "--browser" in raw:
        webbrowser.open(url)
    elif not open_in_window(url, f"gametrans 面板 · {game.name}"):
        print("(没有 pywebview，退回浏览器；想要独立窗口：pip install pywebview)")
        webbrowser.open(url)
        try:
            server.wait()
        except KeyboardInterrupt:
            server.terminate()
        return 0
    else:
        server.terminate()  # 桌面窗口关了，面板服务跟着收摊
        return 0
    try:
        server.wait()
    except KeyboardInterrupt:
        server.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
