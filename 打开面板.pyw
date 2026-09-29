"""gametrans 面板的正经打开方式：双击直接弹面板窗口，不挂黑控制台。

``.pyw`` 由 pythonw（无控制台的 Python）接管，所以双击后看到的第一个东西
就是面板窗口本身；把游戏文件夹拖到本文件图标上，等价于拖到 ``打开面板.bat``
上。项目记在 ``.gametrans/panel.json``：上次开的是哪个，这次还开哪个，
要换就在面板「设置 → 项目」里一键切换或点「浏览…」。

保留 ``打开面板.bat`` 是兜底：个别机器没有 .pyw 关联，或想看控制台报错时用。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

import open_panel  # noqa: E402  (依赖上面把 scripts/ 加进 sys.path)

sys.exit(open_panel.main())
