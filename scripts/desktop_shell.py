"""gametrans 桌面壳（PySide6）：一个常驻窗口，管理多个项目的面板。

定位是"**壳当浏览器**"：壳只管服务与标签页，每个项目的界面仍由它自己的
单项目服务（:mod:`gametrans.web`）提供 —— 内核与网页版零改动。服务直接
托管在壳进程里（每项目一个守护线程跑 ``ThreadingHTTPServer``）：关壳即
全停，不存在孤儿服务；个别项目可单独停（``server.shutdown()``）。

界面与面板同一套 HUD 视觉系统：近黑/纸白双主题（夜间 / 日间 / 跟随系统，
偏好记在 ``panel.json`` 的 ``theme`` 键，与最近列表同一个文件）、方角
1px 边框、数据等宽、坐标网格打底。顶部是一条浏览器式的标题栏 —— 品牌、
标签条、主题钮、窗口钮挤在同一行（hover 有 pywebview 标题栏同款辉光）；
标签页走自绘 TabStrip + QStackedWidget，关钮是文字 ✕，不借系统图标。
面板侧在壳里（``?shell=1``）会收起设置页的主题档位组，由壳在每次页面
加载后注入主题 —— 主题只有壳一个开关，两边不会打架。启动中的标签页是
带扫光过渡条的过渡页，页面能显示了才原地揭开。**标签 ≠ 服务**：关标签
只收 UI，服务照常跑，主页里「去这个标签」开回来；点「停止」（有确认，
正在跑的翻译会被中断）才是停服务，关壳全停。切主题走幕布式过渡
（旧底色盖上来 → 换肤 → 幕布淡去），窗口全程不透明，不会透出桌面。

PySide6 不可用时模块照常可导入（``_QT_OK = False``），:func:`run` 返回 2，
调用方（``open_panel``）退回旧方式（pywebview 窗口 / 浏览器）——
桌面壳是增强，不是门槛。

已知边界（v1 刻意不做）：无边框窗口的**边缘拖拽缩放**（有最大化，够用）；
打包成单文件正式版（PyInstaller，另立工作）；面板内"设置→项目"就地切走后
壳的标签名不跟换（服务托管的还是那个端口，主页认路径不认内容）；壳注入
主题发生在页面加载完成之后，首帧可能先按面板自己记住的主题闪一下。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gametrans.userconfig import panel_state_file  # noqa: E402 - sys.path 先行

#: panel.json 的落点收口在内核（三方共用；冻结版 exe 旁可写就写旁边，
#: 只读退 %LOCALAPPDATA%/GameTrans —— 写不进去又不报错的症状是"最近列表永远为空"）。
PANEL_JSON = panel_state_file()

#: 首次打开一个项目时，等服务就绪的最长时间（秒）。服务是壳内线程，
#: 起得很快；超时基本意味着项目坏了（引擎探测失败之类）。
READY_TIMEOUT = 15.0

#: 面板在壳里打开时带上的标记：设置页据此收起主题档位组（app.js 消费）。
SHELL_FLAG = "?shell=1"

#: 主题偏好取值与面板 applyTheme 的三档一致；壳里点击循环切换。
THEME_PREFS = ("dark", "light", "system")
THEME_LABELS = {"dark": "夜间", "light": "日间", "system": "跟随系统"}
THEME_GLYPHS = {"dark": "●", "light": "○", "system": "◐"}

MONO = '"Cascadia Mono", "Cascadia Code", Consolas, monospace'


def _rgba(hex_color: str, alpha: int) -> str:
    return f"rgba({int(hex_color[1:3], 16)}, {int(hex_color[3:5], 16)}, {int(hex_color[5:7], 16)}, {alpha})"


#: 两套 token 与面板 style.css 的 :root / html[data-theme="light"] 同源；
#: accent 是文字/边框色，fill 是实心填充面（日间把主色压深、填充面另配）。
THEMES: dict[str, dict] = {
    "dark": dict(
        bg="#07090d", bg2="#0b1018", panel="#0e141d", panel2="#131b26",
        line="#1d2735", line2="#2b3849", fg="#e4edf8", soft="#c3cddd",
        dim="#8593a9", faint="#59667c", accent="#cdf94a", fill="#cdf94a",
        fill_hover="#d9fb66", ink="#0b0e05", cyan="#46e5ff", red="#ff5d78",
        close_ink="#ffffff",
        grid=(133, 147, 169, 12), glow=(205, 249, 74, 14), glow2=(70, 229, 255, 13),
    ),
    "light": dict(
        bg="#edefe6", bg2="#f3f5ec", panel="#fbfcf5", panel2="#ffffff",
        line="#d3d7c5", line2="#b9bfa8", fg="#22271a", soft="#3c4433",
        dim="#5d654e", faint="#8b9279", accent="#476e06", fill="#b8e332",
        fill_hover="#c9ee49", ink="#17200a", cyan="#0a7d95", red="#bc1e3e",
        close_ink="#ffffff",
        grid=(93, 101, 78, 18), glow=(184, 227, 50, 26), glow2=(10, 125, 149, 16),
    ),
}

#: 当前生效的 token（GridCanvas / LoadingBar 这类自绘件从这里取色）。
CURRENT: dict = THEMES["dark"]


def recent_projects() -> list[str]:
    """panel.json 里的最近项目（目录已不存在的剔除；文件坏了当没记过）。"""
    try:
        data = json.loads(PANEL_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("recent") if isinstance(data, dict) else None
    out: list[str] = []
    for item in raw or []:
        item = str(item)
        if Path(item).is_dir() and item not in out:
            out.append(item)
    return out


def load_pref(key: str, default: str) -> str:
    """panel.json 里的壳偏好（theme 等）；文件坏了/值不认识都回落默认。"""
    try:
        data = json.loads(PANEL_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    val = data.get(key) if isinstance(data, dict) else None
    return val if val in THEME_PREFS else default


def save_pref(key: str, value: str) -> None:
    """合并写回 panel.json：只动自己的键，最近列表等其余内容原样保留。"""
    try:
        try:
            data = json.loads(PANEL_JSON.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        data[key] = value
        PANEL_JSON.parent.mkdir(parents=True, exist_ok=True)
        PANEL_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # 记不住就只在会话内生效


def system_prefers_light() -> bool:
    """跟随系统：读 Windows 的"应用模式"（注册表）；拿不到就按夜间。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 1
    except Exception:  # noqa: BLE001 - 非 Windows / 注册表读不到，按夜间
        return False


def resolve_theme(pref: str) -> str:
    return "light" if pref == "light" or (pref == "system" and system_prefers_light()) else "dark"


class ServiceHandle:
    """一个项目在壳进程内的面板服务（线程化的单项目服务，与 `gametrans web` 同路径）。"""

    def __init__(self, project: Path) -> None:
        from gametrans.web.app import WebApp
        from gametrans.web.server import create_server

        self.project = project
        self.app = WebApp(project)
        self.server = create_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def wait_ready(url: str, timeout: float = READY_TIMEOUT) -> bool:
    """轮询 /api/ping 直到服务应答（服务线程冷启动通常在一秒内）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/ping", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            time.sleep(0.15)
    return False


try:  # Qt 缺失（未装 PySide6 / 插件坏了）不拦模块导入，只拦窗口
    from PySide6.QtCore import QPoint, QObject, QRectF, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import (
        QColor, QFont, QFontMetrics, QPainter, QPalette, QPen, QRadialGradient,
    )
    from PySide6.QtCore import (
        QAbstractAnimation, QEasingCurve, QPropertyAnimation,
    )
    from PySide6.QtWidgets import (
        QApplication, QFileDialog, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
        QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
        QStackedLayout, QStackedWidget, QVBoxLayout, QWidget,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401

    _QT_OK = True
except Exception:  # noqa: BLE001 - 缺什么都在 run() 里如实报
    _QT_OK = False

if _QT_OK:

    def build_qss(t: dict) -> str:
        return f"""
    QMainWindow, QDialog {{ background: {t['bg']}; }}
    QWidget {{ background: {t['bg']}; color: {t['fg']};
      font-family: "Segoe UI", "Microsoft YaHei"; font-size: 13px; }}
    QLabel {{ background: transparent; }}
    QToolTip {{ background: {t['panel2']}; color: {t['fg']};
      border: 1px solid {t['line2']}; padding: 4px 8px; }}

    /* ---- 按钮：幽灵为常态，主操作实心主色，危险动作悬停见红 ---- */
    QPushButton {{
      background: transparent; color: {t['dim']}; border: 1px solid {t['line2']};
      padding: 6px 16px; font-size: 12.5px;
    }}
    QPushButton:hover {{ color: {t['fg']}; background: {t['hover']}; }}
    QPushButton:pressed {{ background: {t['hover2']}; }}
    QPushButton:disabled {{ color: {t['faint']}; border-color: {t['line']}; background: transparent; }}
    QPushButton#primary {{
      background: {t['fill']}; color: {t['ink']}; border: 1px solid {t['fill']};
      font-weight: 700; padding: 7px 18px;
    }}
    QPushButton#primary:hover {{ background: {t['fill_hover']}; border-color: {t['fill_hover']}; }}
    QPushButton#primary:pressed {{ background: {t['accent']}; color: {t['ink']}; }}
    QPushButton#danger:hover {{ color: {t['red']}; border-color: {t['red']}; background: transparent; }}

    /* ---- 滚动条：细槽圆柄，其余归零 ---- */
    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {t['line2']}; min-height: 30px; border-radius: 5px; }}
    QScrollBar::handle:vertical:hover {{ background: {t['dim']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QMessageBox {{ background: {t['panel']}; }}
    QMessageBox QLabel {{ background: transparent; color: {t['fg']}; font-size: 13px; }}
    QMessageBox QPushButton {{ min-width: 72px; }}

    /* ---- 主页 ---- */
    QLabel#logo-big {{
      background: {t['fill']}; color: {t['ink']}; font-weight: 800;
      min-width: 46px; max-width: 46px; min-height: 46px; max-height: 46px;
      border-radius: 6px; font-size: 19px;
    }}
    QLabel#eyebrow {{ color: {t['accent']}; font-family: {MONO}; font-size: 11px; }}
    QLabel#h1 {{ color: {t['fg']}; font-size: 21px; font-weight: 800; }}
    QLabel#hint {{ color: {t['faint']}; font-size: 12px; }}
    QLabel#chip {{
      color: {t['dim']}; border: 1px solid {t['line']}; background: {t['bg2']};
      font-family: {MONO}; font-size: 11px; padding: 4px 10px;
    }}
    QLabel#footnote {{ color: {t['faint']}; font-family: {MONO}; font-size: 10.5px; }}

    QWidget#card {{ background: {t['panel']}; border: 1px solid {t['line']}; }}
    QWidget#card:hover {{ background: {t['panel2']}; border-color: {t['line2']}; }}
    QWidget#edge {{ background: {t['line']}; }}
    QWidget#edge-on {{ background: {t['fill']}; }}
    QLabel#pname {{ color: {t['fg']}; font-size: 13.5px; font-weight: 700; }}
    QLabel#ppath {{ color: {t['faint']}; font-family: {MONO}; font-size: 10.5px; }}
    QLabel#status-on {{ color: {t['accent']}; font-family: {MONO}; font-size: 11px; }}
    QLabel#status-off {{ color: {t['faint']}; font-family: {MONO}; font-size: 11px; }}
    """

    # hover 洗色从 accent 现算，两套主题不跑样（与面板 color-mix 同思路）
    for _theme in THEMES.values():
        _theme["hover"] = _rgba(_theme["accent"], 13)
        _theme["hover2"] = _rgba(_theme["accent"], 26)

    def build_palette(t: dict) -> "QPalette":
        """Fusion + 主题 palette：QSS 管不到的原生件（对话框、滚动细节）不掉回白底。"""
        p = QPalette()
        p.setColor(QPalette.ColorRole.Window, QColor(t["bg"]))
        p.setColor(QPalette.ColorRole.WindowText, QColor(t["fg"]))
        p.setColor(QPalette.ColorRole.Base, QColor(t["bg2"]))
        p.setColor(QPalette.ColorRole.AlternateBase, QColor(t["panel"]))
        p.setColor(QPalette.ColorRole.Button, QColor(t["panel"]))
        p.setColor(QPalette.ColorRole.ButtonText, QColor(t["fg"]))
        p.setColor(QPalette.ColorRole.Text, QColor(t["fg"]))
        p.setColor(QPalette.ColorRole.ToolTipBase, QColor(t["panel2"]))
        p.setColor(QPalette.ColorRole.ToolTipText, QColor(t["fg"]))
        p.setColor(QPalette.ColorRole.Highlight, QColor(t["fill"]))
        p.setColor(QPalette.ColorRole.HighlightedText, QColor(t["ink"]))
        p.setColor(QPalette.ColorRole.Link, QColor(t["accent"]))
        p.setColor(QPalette.ColorRole.PlaceholderText, QColor(t["faint"]))
        p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(t["faint"]))
        p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(t["faint"]))
        p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(t["faint"]))
        return p

    def titlebar_qss(t: dict) -> str:
        """标题栏一族的规则必须住标题栏自己的样式表：父部件一旦有自己的样式表，
        应用级样式表对这些子部件就不再作数（Qt 的优先级规则）。"""
        return f"""
    QWidget#titlebar {{ background: {t['bg2']}; border-bottom: 1px solid {t['line']}; }}
    QLabel#logo {{
      background: {t['fill']}; color: {t['ink']}; font-weight: 800;
      border-radius: 4px; font-size: 10px; background-clip: padding;
    }}
    QLabel#title {{ color: {t['fg']}; font-size: 12.5px; font-weight: 700;
      background: transparent; }}
    QLabel#version {{ color: {t['faint']}; font-family: {MONO};
      font-size: 10px; background: transparent; }}

    /* 标签片：面板顶栏同款——静默灰字，选中亮字 + 主色下划线 */
    QWidget#chip {{ background: transparent; border-bottom: 2px solid transparent; }}
    QWidget#chip:hover {{ background: {t['hover']}; }}
    QWidget#chip[active="true"] {{ border-bottom: 2px solid {t['accent']}; }}
    QWidget#chip QLabel#chiptext {{ color: {t['dim']}; font-size: 12.5px;
      background: transparent; }}
    QWidget#chip[active="true"] QLabel#chiptext {{ color: {t['fg']}; font-weight: 600; }}
    QPushButton#chipx {{
      background: transparent; border: none; color: {t['faint']}; padding: 0;
      font-size: 10px; min-width: 18px; max-width: 18px;
      min-height: 18px; max-height: 18px; border-radius: 3px;
    }}
    QPushButton#chipx:hover {{ background: {t['red']}; color: {t['close_ink']}; }}
    """

    class GlowButton(QPushButton):
        """窗口钮/主题钮：完全自绘，hover 时整个框发光（pywebview 标题栏同款）。

        不用 QGraphicsDropShadowEffect：它默认向下偏 8px，还会把按钮缓存成
        位图、QSS 的 hover 重绘跟不上。发光的实现是按钮四周留 MARGIN 呼吸边，
        从框缘向外 1px 一圈、平方衰减 —— 圈够密，读起来就是光而不是线。
        """

        MARGIN = 5  # 框外辉光的呼吸空间（px）；不够会被相邻按钮裁掉

        def __init__(self, text: str, glow: str, parent: QWidget | None = None) -> None:
            super().__init__(text, parent)
            self._glow_kind = glow  # accent / close
            self._hover = False
            self.setFlat(True)

        def enterEvent(self, event) -> None:  # noqa: N802
            self._hover = True
            self.update()

        def leaveEvent(self, event) -> None:  # noqa: N802
            self._hover = False
            self.update()

        def paintEvent(self, event) -> None:  # noqa: N802
            t = CURRENT
            p = QPainter(self)
            close = self._glow_kind == "close"
            box = self.rect().adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
            if self._hover and self.isEnabled():
                ring_c = QColor(t["red"] if close else t["fill"])
                for d in range(self.MARGIN, 0, -1):  # 由外向内：越贴框越亮
                    alpha = min(int(170 * (1 - (d - 1) / self.MARGIN) ** 1.6) + 10, 255)
                    c = QColor(ring_c)
                    c.setAlpha(alpha)
                    p.setPen(QPen(c, 1))
                    p.drawRect(box.adjusted(-d, -d, d, d))
                p.fillRect(box, QColor(t["red"]) if close else QColor(t["line"]))
                ink = QColor(t["close_ink"]) if close else QColor(t["fg"])
            else:
                ink = QColor(t["dim"])
            font = QFont(self.font())
            font.setPixelSize(13)
            p.setFont(font)
            p.setPen(QPen(ink, 1))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, self.text())

    class GridCanvas(QWidget):
        """主页/过渡页的画布：坐标网格 + 两团辉光，与面板 body 同一套底子。"""

        def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
            t = CURRENT
            p = QPainter(self)
            w, h = self.width(), self.height()
            p.fillRect(self.rect(), QColor(t["bg"]))
            p.setPen(QPen(QColor(*t["grid"]), 1))
            step = 34
            for x in range(step, w, step):
                p.drawLine(x, 0, x, h)
            for y in range(step, h, step):
                p.drawLine(0, y, w, y)
            glow = QRadialGradient(w * 0.12, -h * 0.06, min(620, w * 0.7))
            glow.setColorAt(0.0, QColor(*t["glow"]))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(QRectF(0, 0, w, h * 0.55), glow)
            glow2 = QRadialGradient(w * 1.05, h * 1.1, min(560, w * 0.6))
            glow2.setColorAt(0.0, QColor(*t["glow2"]))
            glow2.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(QRectF(w * 0.4, h * 0.5, w * 0.6, h * 0.5), glow2)

    class LoadingBar(QWidget):
        """过渡页的扫光条：暗槽里一段主色来回扫，服务就绪就整页换走。"""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setFixedSize(240, 4)
            self._t = 0.0
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)

        def _tick(self) -> None:
            self._t = (self._t + 0.022) % 2.0
            self.update()

        def showEvent(self, event) -> None:  # noqa: N802
            self._timer.start(28)

        def hideEvent(self, event) -> None:  # noqa: N802
            self._timer.stop()

        def paintEvent(self, event) -> None:  # noqa: N802
            t = CURRENT
            p = QPainter(self)
            w, h = self.width(), self.height()
            p.fillRect(0, 0, w, h, QColor(t["line"]))
            seg = int(w * 0.32)
            pos = self._t if self._t < 1.0 else 2.0 - self._t  # 三角波：来回扫
            x = int(pos * (w - seg))
            p.fillRect(x, 0, seg, h, QColor(t["fill"]))
            trail = QColor(t["fill"])
            trail.setAlpha(60)
            p.fillRect(max(x - 14, 0), 0, 14, h, trail)
            p.fillRect(min(x + seg, w - 14), 0, 14, h, trail)

    class TabChip(QWidget):
        """标题栏里的一片标签：名字 + 关钮。active 属性驱动 QSS。"""

        clicked = Signal()
        closed = Signal()

        def __init__(self, title: str, closable: bool, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self.setObjectName("chip")
            self.setProperty("active", False)
            self.setFixedHeight(30)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            row = QHBoxLayout(self)
            row.setContentsMargins(10, 0, 5, 0)
            row.setSpacing(5)
            self.label = QLabel(title)
            self.label.setObjectName("chiptext")
            fm = QFontMetrics(self.label.font())
            self.label.setText(fm.elidedText(title, Qt.TextElideMode.ElideMiddle, 150))
            row.addWidget(self.label)
            self.close_btn: QPushButton | None = None
            if closable:
                self.close_btn = QPushButton("✕")
                self.close_btn.setObjectName("chipx")
                self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                self.close_btn.clicked.connect(self.closed.emit)
                row.addWidget(self.close_btn)

        def set_active(self, on: bool) -> None:
            self.setProperty("active", on)
            self.style().unpolish(self)
            self.style().polish(self)

        def mouseReleaseEvent(self, event) -> None:  # noqa: N802
            if event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit()

    class TabStrip(QWidget):
        """标签条：主页片固定第一片，项目片随开随关。"""

        activated = Signal(str)
        close_requested = Signal(str)

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.chips: dict[str, TabChip] = {}
            row = QHBoxLayout(self)
            row.setContentsMargins(12, 8, 0, 8)
            row.setSpacing(4)
            row.setAlignment(Qt.AlignmentFlag.AlignTop)
            home = TabChip("主页", closable=False)
            home.clicked.connect(lambda: self.activated.emit("home"))
            row.addWidget(home)
            row.addStretch(1)  # 标签片保持自身宽度，不跟标题栏一起拉伸
            self.chips["home"] = home

        def add_tab(self, key: str, title: str) -> None:
            chip = TabChip(title, closable=True)
            chip.clicked.connect(lambda k=key: self.activated.emit(k))
            chip.closed.connect(lambda k=key: self.close_requested.emit(k))
            # 收尾的 stretch 永远垫底：新标签插到它前面，跟在主页片后面
            self.layout().insertWidget(self.layout().count() - 1, chip)
            self.chips[key] = chip

        def remove_tab(self, key: str) -> None:
            chip = self.chips.pop(key, None)
            if chip is not None:
                self.layout().removeWidget(chip)
                chip.deleteLater()

        def set_active(self, key: str) -> None:
            for k, chip in self.chips.items():
                chip.set_active(k == key)

    class TitleBar(QWidget):
        """浏览器式标题栏（46px）：品牌 + 标签条 + 主题钮 + 窗口钮 + 拖拽。"""

        HEIGHT = 46

        def __init__(self, window: "ShellWindow") -> None:
            super().__init__(window)
            self.window_ref = window
            self._drag_at: QPoint | None = None
            self.setFixedHeight(self.HEIGHT)
            self.setObjectName("titlebar")
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            bar = QHBoxLayout(self)
            bar.setContentsMargins(12, 0, 0, 0)
            bar.setSpacing(8)
            logo = QLabel("GT")
            logo.setObjectName("logo")
            logo.setFixedSize(24, 24)
            logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title = QLabel("gametrans")
            title.setObjectName("title")
            self.version = QLabel("")
            self.version.setObjectName("version")
            try:
                from gametrans import __version__

                self.version.setText(f"v{__version__}")
            except Exception:  # noqa: BLE001 - 版本拿不到不拦壳
                pass
            bar.addWidget(logo)
            bar.addWidget(title)
            bar.addWidget(self.version)
            self.strip = TabStrip()
            bar.addWidget(self.strip, 1)
            self.theme_btn = GlowButton("●", glow="accent")
            self.theme_btn.setObjectName("themebtn")
            self.theme_btn.setFixedSize(40, self.HEIGHT)
            self.theme_btn.setToolTip("主题")
            self.theme_btn.clicked.connect(window.cycle_theme)
            bar.addWidget(self.theme_btn)
            self.maxbtn: QPushButton | None = None
            for name, mark, cb in (
                ("minbtn", "—", window.showMinimized),
                ("maxbtn", "▢", window.toggle_max),
                ("closebtn", "✕", window.close),
            ):
                btn = GlowButton(mark, glow="close" if name == "closebtn" else "accent")
                btn.setObjectName("winbtn" if name != "closebtn" else "winbtn closebtn")
                btn.setFixedSize(48, self.HEIGHT)
                btn.clicked.connect(lambda _=False, fn=cb: fn())
                if name == "maxbtn":
                    self.maxbtn = btn
                bar.addWidget(btn)
            self.apply_tokens()

        def apply_tokens(self) -> None:
            """主题切了就整套重贴（含标签片/关钮/窗口钮）。"""
            self.setStyleSheet(titlebar_qss(CURRENT))

        def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_at = (
                    event.globalPosition().toPoint() - self.window_ref.frameGeometry().topLeft()
                )

        def mouseMoveEvent(self, event) -> None:  # noqa: N802
            if self._drag_at is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.window_ref.move(event.globalPosition().toPoint() - self._drag_at)

        def mouseReleaseEvent(self, event) -> None:  # noqa: N802
            self._drag_at = None

        def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
            # 空白处双击 = 最大化/还原；落点在标签片上就不抢（chip 自己消费点击）
            if self.childAt(event.position().toPoint()) is self.strip:
                self.window_ref.toggle_max()

    class ProjectCard(QWidget):
        """项目库里的一个项目：状态边条 + 名字/路径 + 状态读数 + 打开/停止。

        整卡可点（等于"打开"）；停止是危险方向的动作，悬停见红，且点击后
        有一次确认（见 ShellWindow.stop_project）。
        """

        def __init__(self, path: str, on_open, on_stop, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.path = path
            self._on_open = on_open
            self._on_stop = on_stop
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self.setObjectName("card")
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setFixedHeight(64)

            row = QHBoxLayout(self)
            row.setContentsMargins(0, 0, 12, 0)
            row.setSpacing(12)
            self.edge = QWidget()
            self.edge.setFixedWidth(3)
            self.edge.setObjectName("edge")
            row.addWidget(self.edge)

            text = QVBoxLayout()
            text.setSpacing(2)
            text.addStretch(1)
            self.name = QLabel(Path(path).name)
            self.name.setObjectName("pname")
            self.where = QLabel(path)
            self.where.setObjectName("ppath")
            fm = QFontMetrics(self.where.font())
            self.where.setText(fm.elidedText(path, Qt.TextElideMode.ElideMiddle, 460))
            text.addWidget(self.name)
            text.addWidget(self.where)
            text.addStretch(1)
            row.addLayout(text, 1)

            self.status = QLabel("未运行")
            self.status.setObjectName("status-off")
            row.addWidget(self.status)
            self.open_btn = QPushButton("打开")
            self.open_btn.setObjectName("primary")
            self.open_btn.clicked.connect(lambda _=False: self._on_open())
            row.addWidget(self.open_btn)
            self.stop_btn = QPushButton("停止")
            self.stop_btn.setObjectName("danger")
            self.stop_btn.setEnabled(False)
            self.stop_btn.clicked.connect(lambda _=False: self._on_stop())
            row.addWidget(self.stop_btn)

        def set_running(self, port: int | None) -> None:
            on = port is not None
            self.edge.setObjectName("edge-on" if on else "edge")
            if on:
                self.status.setText(f"● 运行中 · :{port}")
                self.status.setObjectName("status-on")
                self.stop_btn.setEnabled(True)
                self.open_btn.setText("去这个标签")
                self.open_btn.setObjectName("")
            else:
                self.status.setText("未运行")
                self.status.setObjectName("status-off")
                self.stop_btn.setEnabled(False)
                self.open_btn.setText("打开")
                self.open_btn.setObjectName("primary")
            # objectName 动了，QSS 要重算
            for w in (self.edge, self.status, self.open_btn):
                w.style().unpolish(w)
                w.style().polish(w)

        def mouseReleaseEvent(self, event) -> None:  # noqa: N802
            if event.button() == Qt.MouseButton.LeftButton:
                self._on_open()

    class EmptyHome(QWidget):
        """空态：居中 logo + 一句引导 + 主操作，占满整个列表区。"""

        def __init__(self, on_browse, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            col = QVBoxLayout(self)
            col.addStretch(2)
            logo = QLabel("GT")
            logo.setObjectName("logo-big")
            logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
            col.addSpacing(14)
            title = QLabel("还没有项目")
            title.setStyleSheet("font-size:16px; font-weight:800;")
            col.addWidget(title, 0, Qt.AlignmentFlag.AlignHCenter)
            col.addSpacing(6)
            sentence = QLabel(
                "还没有项目。点下面的「打开新目录…」选一个游戏工程，它会开进一个新标签页。"
            )
            sentence.setObjectName("hint")
            sentence.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(sentence, 0, Qt.AlignmentFlag.AlignHCenter)
            col.addSpacing(18)
            browse = QPushButton("打开新目录…")
            browse.setObjectName("primary")
            browse.setCursor(Qt.CursorShape.PointingHandCursor)
            browse.clicked.connect(lambda _=False: on_browse())
            col.addWidget(browse, 0, Qt.AlignmentFlag.AlignHCenter)
            col.addStretch(3)

    class StartingView(GridCanvas):
        """过渡页：logo + 项目名 + 扫光条。服务是壳内线程，通常一秒内就绪。"""

        def __init__(self, name: str, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            col = QVBoxLayout(self)
            col.addStretch(2)
            logo = QLabel("GT")
            logo.setObjectName("logo-big")
            logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
            col.addSpacing(16)
            who = QLabel(f"正在启动 {name}")
            who.setStyleSheet("font-size:15px; font-weight:700;")
            who.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(who, 0, Qt.AlignmentFlag.AlignHCenter)
            col.addSpacing(14)
            col.addWidget(LoadingBar(), 0, Qt.AlignmentFlag.AlignHCenter)
            col.addSpacing(12)
            note = QLabel("探测引擎 · 准备面板服务")
            note.setObjectName("hint")
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(note, 0, Qt.AlignmentFlag.AlignHCenter)
            col.addStretch(3)

    class _StartDone(QObject):
        #: (项目路径, ServiceHandle|None, 错误话) —— 工作线程把启动结果带回 UI 线程
        done = Signal(str, object, str)

    class ProjectPage(QWidget):
        """一个项目标签页的固定容器：底层是过渡页，面板视图加载完原地揭开。

        不做"删过渡页、插 webview"的页面增删 —— 那一瞬 stack 会闪一帧空底。
        """

        def __init__(self, name: str, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.lay = QStackedLayout(self)
            self.loading = StartingView(name)
            self.lay.addWidget(self.loading)
            self.view: QWebEngineView | None = None

        def attach(self, view: QWebEngineView) -> None:
            self.view = view
            self.lay.addWidget(view)

        def reveal(self) -> None:
            if self.view is not None:
                self.lay.setCurrentWidget(self.view)

    class ShellWindow(QMainWindow):
        """主窗口：浏览器式标题栏（含标签条）+ 页面栈。"""

        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("gametrans 桌面版")
            self.resize(1280, 860)
            self.setMinimumSize(880, 560)
            self.setWindowFlags(
                Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
            )
            self.services: dict[str, ServiceHandle] = {}
            self.views: dict[str, QWidget] = {}
            self._starting: set[str] = set()
            self._cancelled: set[str] = set()  # 启动途中被关掉的标签：服务起完直接停
            self._loading: set[str] = set()  # 服务已就绪、面板页面还没加载完
            self._closing = False
            self._maximized = False
            self.theme_pref = "dark"

            central = QWidget()
            outer = QVBoxLayout(central)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)
            self.title_bar = TitleBar(self)
            outer.addWidget(self.title_bar)
            self.stack = QStackedWidget()
            outer.addWidget(self.stack, 1)
            self.setCentralWidget(central)

            self.stack.addWidget(self._build_home())
            self.title_bar.strip.activated.connect(self._activate_tab)
            self.title_bar.strip.close_requested.connect(self._close_tab)
            self.title_bar.strip.set_active("home")
            self._refresh_home()
            self._center_on_screen()
            # WebEngine 首个视图会让主窗口重建一次原生窗口（视觉上像整窗重启/
            # 先缩成一小块空白）。预热视图必须是主窗口自己的子部件，开局就在：
            # 主窗口从 show() 起就带着合成层，后面塞真面板不再重建。1px 大小
            # 贴在底缘，暗色上看不见；独立屏幕外窗口建的上下文救不了本窗口。
            self._warmup = QWebEngineView(central)
            self._warmup.setFixedSize(1, 1)
            self._warmup.move(0, self.height() - 1)
            self._warmup.load(QUrl("about:blank"))

        def _center_on_screen(self) -> None:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(
                screen.center().x() - self.width() // 2,
                screen.center().y() - self.height() // 2,
            )

        # ---- 主题（夜间 / 日间 / 跟随系统；偏好记在 panel.json） ----

        def toggle_max(self) -> None:
            if self._maximized:
                self.showNormal()
            else:
                self.showMaximized()
            self._maximized = not self._maximized
            if self.title_bar.maxbtn is not None:
                self.title_bar.maxbtn.setText("❐" if self._maximized else "▢")

        def cycle_theme(self) -> None:
            nxt = THEME_PREFS[(THEME_PREFS.index(self.theme_pref) + 1) % len(THEME_PREFS)]
            save_pref("theme", nxt)
            self.apply_theme(nxt, animate=True)

        def apply_theme(self, pref: str, animate: bool = False) -> None:
            self.theme_pref = pref
            if THEMES[resolve_theme(pref)] is CURRENT:
                # 解析出来是同一套配色（如 夜间 → 跟随系统而系统正是夜间）：
                # 只换档位显示 —— 同色还做整窗淡出，只会透出桌面
                self._sync_theme_button()
                return
            if animate and self.isVisible():
                if getattr(self, "_fading", False):
                    self._apply_theme_now(pref)  # 动画中再点：直接落定，不叠动画
                    return
                self._fade_to(pref)
            else:
                self._apply_theme_now(pref)

        def _sync_theme_button(self) -> None:
            glyph = THEME_GLYPHS[self.theme_pref]
            self.title_bar.theme_btn.setText(glyph)
            self.title_bar.theme_btn.update()
            self.title_bar.theme_btn.setToolTip(
                f"主题：{THEME_LABELS[self.theme_pref]}（点击切换 夜间 → 日间 → 跟随系统）"
            )

        def _fade_to(self, pref: str) -> None:
            """幕布式换肤：旧底色的幕布盖上来 → 换肤 → 幕布淡去露出新皮肤。

            不动整窗透明度 —— 那会透出桌面（同色切都救不回来，异色切必现）。
            """
            self._fading = True
            veil = QLabel(self.centralWidget())
            veil.setStyleSheet(f"background: {CURRENT['bg']}; border: none;")
            central = self.centralWidget()
            veil.setGeometry(central.rect())
            veil.show()
            veil.raise_()
            eff = QGraphicsOpacityEffect(veil)
            eff.setOpacity(0.0)
            veil.setGraphicsEffect(eff)
            cover = QPropertyAnimation(eff, b"opacity", self)
            cover.setDuration(150)
            cover.setStartValue(0.0)
            cover.setEndValue(1.0)
            cover.setEasingCurve(QEasingCurve.Type.InQuad)

            def swap() -> None:
                self._apply_theme_now(pref)
                reveal = QPropertyAnimation(eff, b"opacity", self)
                reveal.setDuration(240)
                reveal.setStartValue(1.0)
                reveal.setEndValue(0.0)
                reveal.setEasingCurve(QEasingCurve.Type.OutQuad)
                reveal.finished.connect(veil.deleteLater)
                reveal.finished.connect(lambda: setattr(self, "_fading", False))
                reveal.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

            cover.finished.connect(swap)
            cover.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

        def _apply_theme_now(self, pref: str) -> None:
            self.theme_pref = pref
            global CURRENT
            CURRENT = THEMES[resolve_theme(pref)]
            app = QApplication.instance()
            app.setPalette(build_palette(CURRENT))
            app.setStyleSheet(build_qss(CURRENT))
            self.title_bar.apply_tokens()
            self._sync_theme_button()
            self.update()
            for page in self.views.values():
                if isinstance(page, ProjectPage) and page.view is not None:
                    self._inject_theme(page.view)

        @staticmethod
        def _theme_js(pref: str) -> str:
            return (
                f"document.documentElement.dataset.pref={json.dumps(pref)};"
                f"document.documentElement.dataset.theme={json.dumps(resolve_theme(pref))};void 0;"
            )

        def _inject_theme(self, view: QWebEngineView) -> None:
            view.page().runJavaScript(self._theme_js(self.theme_pref))

        # ---- 主页 ----

        def _build_home(self) -> QWidget:
            home = GridCanvas()
            outer = QVBoxLayout(home)
            outer.setContentsMargins(28, 24, 28, 18)
            outer.setSpacing(14)

            head = QHBoxLayout()
            head.setSpacing(10)
            text = QVBoxLayout()
            text.setSpacing(4)
            eyebrow = QLabel("gt://projects")
            eyebrow.setObjectName("eyebrow")
            h1 = QLabel("项目库")
            h1.setObjectName("h1")
            text.addWidget(eyebrow)
            text.addWidget(h1)
            head.addLayout(text)
            head.addStretch(1)
            self.chip_total = QLabel("项目 0")
            self.chip_total.setObjectName("chip")
            self.chip_run = QLabel("运行中 0")
            self.chip_run.setObjectName("chip")
            head.addWidget(self.chip_total)
            head.addWidget(self.chip_run)
            outer.addLayout(head)

            hint = QLabel(
                "「打开」把项目开进一个新标签页；关标签不停服务，主页里点「停止」才停（正在跑的翻译会被中断）；关壳即全停。"
            )
            hint.setObjectName("hint")
            outer.addWidget(hint)

            self.rows_box = QWidget()
            self.rows = QVBoxLayout(self.rows_box)
            self.rows.setContentsMargins(0, 2, 0, 2)
            self.rows.setSpacing(8)
            scroll = QScrollArea()
            scroll.setWidget(self.rows_box)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            outer.addWidget(scroll, 1)

            buttons = QHBoxLayout()
            browse = QPushButton("打开新目录…")
            browse.setObjectName("primary")
            browse.setCursor(Qt.CursorShape.PointingHandCursor)
            browse.clicked.connect(self._browse)
            refresh = QPushButton("刷新")
            refresh.setCursor(Qt.CursorShape.PointingHandCursor)
            refresh.clicked.connect(self._refresh_home)
            buttons.addWidget(browse)
            buttons.addWidget(refresh)
            buttons.addStretch(1)
            footnote = QLabel("服务托管在壳内 · 关壳即全停")
            footnote.setObjectName("footnote")
            buttons.addWidget(footnote)
            outer.addLayout(buttons)
            return home

        def _refresh_home(self) -> None:
            """按「正在运行 ∪ 最近打开」重排项目卡片（服务状态随时会变）。"""
            while (item := self.rows.takeAt(0)) is not None:
                if w := item.widget():
                    w.deleteLater()
            ordered = list(self.services)
            for p in recent_projects():
                if p not in ordered:
                    ordered.append(p)
            self.chip_total.setText(f"项目 {len(ordered)}")
            self.chip_run.setText(f"运行中 {len(self.services)}")
            if not ordered:
                self.rows.addWidget(EmptyHome(self._browse), 1)
            else:
                for path in ordered:
                    card = ProjectCard(
                        path,
                        on_open=lambda p=path: self.open_project(Path(p)),
                        on_stop=lambda p=path: self.stop_project(p),
                    )
                    svc = self.services.get(path)
                    card.set_running(svc.port if svc else None)
                    self.rows.addWidget(card)
                self.rows.addStretch(1)

        def _browse(self) -> None:
            picked = QFileDialog.getExistingDirectory(self, "选择游戏目录")
            if picked:
                self.open_project(Path(picked))

        # ---- 项目的开与停（启动全程在后台线程，UI 只见过渡态） ----
        # 语义：标签 ≠ 服务。关标签只收 UI，服务照常跑；主页里点「停止」
        # （有确认）才是停服务；关壳全停。

        def open_project(self, game: Path) -> None:
            project = str(game.expanduser().resolve())
            if not Path(project).is_dir():
                QMessageBox.warning(self, "gametrans", f"不是目录：\n{project}")
                return
            if project in self.services:
                self.open_tab_for(project)  # 服务还在跑：只开标签，不动进程
                return
            if project in self._starting:
                return  # 正在启动：过渡标签页已经在路上
            name = Path(project).name
            placeholder = ProjectPage(name)
            self.views[project] = placeholder
            self.stack.addWidget(placeholder)
            self.title_bar.strip.add_tab(project, name)
            self._show_view(project)
            self._starting.add(project)

            done = _StartDone()
            done.done.connect(self._start_finished)
            threading.Thread(
                target=self._start_worker, args=(project, done), daemon=True
            ).start()

        def open_tab_for(self, project: str) -> None:
            """给一个已在跑的服务开回它的标签页（主页「去这个标签」走这里）。"""
            handle = self.services.get(project)
            if handle is None:
                self.open_project(Path(project))
                return
            if project in self.views:
                self._show_view(project)
                return
            name = Path(project).name
            page = ProjectPage(name)
            self.views[project] = page
            self.stack.addWidget(page)
            self.title_bar.strip.add_tab(project, name)
            self._spawn_view(project, handle)
            self._show_view(project)

        def _show_view(self, project: str) -> None:
            view = self.views.get(project)
            if view is not None:
                self.stack.setCurrentWidget(view)
                self.title_bar.strip.set_active(project)

        def _activate_tab(self, key: str) -> None:
            if key == "home":
                self.stack.setCurrentIndex(0)
                self.title_bar.strip.set_active("home")
            else:
                self._show_view(key)

        def _start_worker(self, project: str, done: _StartDone) -> None:
            """重活全在这（建 WebApp、探测引擎、等服务就绪）—— 不碰 UI 线程。"""
            handle = None
            error = ""
            try:
                import open_panel  # noqa: PLC0415  初始化与最近列表都跟 launcher 同一份

                open_panel.ensure_project(Path(project))
                handle = ServiceHandle(Path(project))
                if not wait_ready(handle.url):
                    handle.stop()
                    handle = None
                    error = "服务起了但迟迟不应答，已停掉。"
                else:
                    open_panel.remember_project(Path(project))
            except Exception as exc:  # noqa: BLE001 - 给一句人话，不堆栈
                if handle is not None:
                    handle.stop()
                    handle = None
                error = str(getattr(exc, "message", None) or exc)
            done.done.emit(project, handle, error)

        def _start_finished(self, project: str, handle: ServiceHandle | None, error: str) -> None:
            self._starting.discard(project)
            page = self.views.get(project)
            if self._closing or project in self._cancelled:
                self._cancelled.discard(project)
                if handle is not None:
                    handle.stop()
                return
            if handle is None:
                if page is not None:
                    self._drop_tab(project, page)
                self._refresh_home()
                QMessageBox.warning(self, "打不开这个项目", error or "未知错误")
                return
            self.services[project] = handle
            self._spawn_view(project, handle)
            self._refresh_home()

        def _spawn_view(self, project: str, handle: ServiceHandle) -> None:
            """建 webview 挂进 ProjectPage 后台加载；页面能显示了才原地揭开。"""
            view = QWebEngineView()
            view.page().setBackgroundColor(QColor(CURRENT["bg"]))
            view.loadFinished.connect(lambda _ok, v=view: self._inject_theme(v))
            view.loadFinished.connect(lambda _ok, p=project: self._page_ready(p))
            view.load(handle.url + "/" + SHELL_FLAG + "#/overview")
            page = self.views.get(project)
            if page is not None:
                page.attach(view)
            self._loading.add(project)

        def _page_ready(self, project: str) -> None:
            self._loading.discard(project)
            page = self.views.get(project)
            if page is None or page.view is None:
                return
            if self.stack.currentWidget() is page:
                page.reveal()  # 用户还停在这个标签：原地揭开；不在就不抢焦点

        def _drop_tab(self, project: str, page: QWidget) -> None:
            """只收标签 UI；服务归 stop_project / 关壳管。"""
            if project in self._starting:
                self._cancelled.add(project)
            self._loading.discard(project)
            self.title_bar.strip.remove_tab(project)
            self.stack.removeWidget(page)
            if self.views.get(project) is page:
                self.views.pop(project, None)

        def _close_tab(self, project: str) -> None:
            """标签上的 ✕：只关标签，不停服务（主页里还能「去这个标签」开回来）。"""
            page = self.views.get(project)
            if page is None:
                return
            if project in self._starting:
                self.stop_project(project)  # 起动途中关标签 = 掐掉这次启动
                return
            was_active = self.stack.currentWidget() is page
            self._drop_tab(project, page)
            page.deleteLater()  # 连着里面挂着的 webview 一起销毁；服务不受影响
            if was_active or not self.views:
                self.stack.setCurrentIndex(0)
                self.title_bar.strip.set_active("home")
            self._refresh_home()

        def stop_service(self, project: str) -> None:
            svc = self.services.pop(project, None)
            if svc is not None:
                svc.stop()

        def stop_project(self, project: str) -> None:
            name = Path(project).name
            if project in self.services:
                sure = QMessageBox.question(
                    self,
                    "停止服务",
                    f"停掉 {name} 的面板服务？\n正在进行的翻译任务会被中断。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if sure != QMessageBox.StandardButton.Yes:
                    return
            self.stop_service(project)
            page = self.views.get(project)
            was_active = page is not None and self.stack.currentWidget() is page
            if page is not None:
                self._drop_tab(project, page)
                page.deleteLater()  # 连着里面挂着的 webview 一起销毁
            if was_active or not self.views:
                self.stack.setCurrentIndex(0)
                self.title_bar.strip.set_active("home")
            self._refresh_home()

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
            self._closing = True
            for project in list(self.services):
                self.stop_service(project)
            super().closeEvent(event)


def run(game: Path | None = None) -> int:
    """起桌面壳；PySide6 缺失或起不来时返回 2（调用方退回旧方式）。"""
    if not _QT_OK:
        print("(桌面壳不可用：PySide6 没装好（pip install PySide6），退回原方式)")
        return 2
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    qt = QApplication(sys.argv)
    qt.setApplicationName("gametrans 桌面版")
    qt.setStyle("Fusion")
    window = ShellWindow()
    # 启动必须走完整换肤：apply_theme 对"目标配色 == 当前配色"会跳过
    # （那是对点击切换说的），夜间启动时 CURRENT 本来就是夜间 token，
    # 跳过 = QSS/调色板从未应用 = 毛坯房。
    window._apply_theme_now(load_pref("theme", "dark"))
    if game is not None:
        window.open_project(game)
    window.show()
    return qt.exec()


if __name__ == "__main__":
    raw = [a for a in sys.argv[1:] if not a.startswith("-")]
    game = Path(raw[0]).expanduser() if raw else None
    sys.exit(run(game))
