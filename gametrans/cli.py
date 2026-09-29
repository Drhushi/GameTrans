"""CLI 控制面 —— agent 驱动软件的主要入口。

两个输出面是分开的：

* ``--json``：结构化结果给 **agent**，含全部细节与结构化错误；
* 默认输出：给 **用户**，走交互层的可见性策略，只显示"用户关心的"。

子命令由 :mod:`gametrans.operations` 的注册表生成，因此 CLI 与 MCP 永远不会走偏。
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import Any, Sequence

from gametrans.errors import GameTransError
from gametrans.operations import (
    Context,
    Operation,
    Param,
    arguments_for,
    error_envelope,
    json_dumps,
    operation_tree,
    result_envelope,
)

PROG = "gametrans"


# --------------------------------------------------------------------------- #
# 解析器构造
# --------------------------------------------------------------------------- #


def _csv_arg(value: str) -> list[str]:
    return [piece.strip() for piece in value.split(",") if piece.strip()]


def _add_param(parser: argparse.ArgumentParser, param: Param) -> None:
    kwargs: dict[str, Any] = {"help": param.help or param.name}
    if param.positional:
        if param.choices:
            kwargs["choices"] = param.choices
        if param.type == "csv":
            kwargs["type"] = _csv_arg
        if not param.required:
            # 位置参数在 argparse 里缺省就是必填；没标必填的允许留空
            # （例如 `resource term add` 的两栏都可以改用 `--key` 那一条完整写法）。
            kwargs["nargs"] = "?"
        parser.add_argument(param.name, **kwargs)
        return

    flag = param.flag or ("--" + param.name.replace("_", "-"))
    kwargs["dest"] = param.name
    if param.type == "bool":
        kwargs["action"] = "store_true"
        kwargs["default"] = False
    else:
        kwargs["default"] = param.default
        if param.choices:
            kwargs["choices"] = param.choices
        if param.type == "int":
            kwargs["type"] = int
        if param.type == "csv":
            kwargs["type"] = _csv_arg
        if param.required:
            kwargs["required"] = True
    parser.add_argument(flag, **kwargs)


def _build_commands(container: argparse.ArgumentParser, tree: dict[str, Any]) -> None:
    subparsers = container.add_subparsers(dest="command", metavar="<命令>")
    for key in sorted(tree):
        node = tree[key]
        if isinstance(node, Operation):
            sub = subparsers.add_parser(
                key, help=node.summary, description=node.summary
            )
            for param in node.params:
                _add_param(sub, param)
            sub.set_defaults(operation=node)
        else:
            sub = subparsers.add_parser(key, help=f"{key} 相关命令")
            _build_commands(sub, node)
    subparsers.required = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "AI 驱动的游戏翻译工作台。\n"
            "五层：提取 / 翻译 / 写回 / 资源 / 交互，每一层都可以单独驱动。"
        ),
        epilog=(
            "示例：\n"
            f"  {PROG} project init ./MyGame --target-language zh_CN\n"
            f"  {PROG} --json scan\n"
            f"  {PROG} --json translate --provider openai --batch-size 10\n"
            f"  {PROG} --json writeback && {PROG} --json pack\n"
            f"  {PROG} --json ui views --all\n"
            f"  {PROG} web          # 启动本地只读面板\n"
            f"  {PROG} mcp          # 以 stdio MCP server 形式启动，供 agent 直接调用\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-p", "--project", default=".", help="游戏项目根目录（默认当前目录）")
    parser.add_argument("--workdir", default=None, help="工作区目录（默认 <项目>/.gametrans）")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON（给 agent 用）")
    parser.add_argument("--quiet", action="store_true", help="不打印人类可读信息")
    parser.add_argument("--debug", action="store_true", help="出错时展开完整堆栈")
    _build_commands(parser, operation_tree())
    return parser


# --------------------------------------------------------------------------- #
# 人类可读渲染
# --------------------------------------------------------------------------- #


def _scalar(value: Any) -> str:
    if value is None:
        return "（无）"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _render_data(data: Any, indent: int = 0) -> str:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{key}:")
                lines.append(_render_data(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(value)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                lines.append(f"{pad}- {_render_data(item, indent + 1).lstrip()}")
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:
        lines.append(f"{pad}{_scalar(data)}")
    return "\n".join(line for line in lines if line != "")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def _exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    return 2


def _report_error(err, ctx: Context, exc: BaseException) -> int:
    payload = error_envelope(exc)
    if ctx.json_mode:
        print(json_dumps(payload), file=err)
    else:
        print(f"错误：{payload['error']['message']}", file=err)
        if payload["error"]["hint"]:
            print(f"提示：{payload['error']['hint']}", file=err)
    return getattr(exc, "exit_code", 1)


#: 这些全局 flag 会吃掉后面一个 token，找子命令时要跳过它们的值
_VALUE_FLAGS = {"--project", "-p", "--workdir", "--port"}

#: 传输通道命令：它们是"怎么跟软件说话"，不是"让软件做什么"，所以不进操作注册表
TRANSPORT_COMMANDS = ("mcp", "web")


def _leading_command(argv: Sequence[str]) -> tuple[str | None, int]:
    """找出 argv 里第一个真正的子命令 token 及其下标。

    CLI 的约定是全局 flag 在前（``gametrans --project X scan``），所以传输命令
    也必须能在 flag 之后被认出来，不能只看 ``argv[0]``。
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            # 自带取值的写法（--project=X）或纯开关，都只占一个 token
            index += 1
            continue
        return token, index
    return None, -1


def _run_web(argv: Sequence[str], out, err) -> int:
    """启动本地面板。和 ``mcp`` 一样是传输通道而不是一个操作，所以不走操作注册表。"""
    from gametrans.web.app import WebApp
    from gametrans.web.server import DEFAULT_HOST, DEFAULT_PORT, serve

    parser = argparse.ArgumentParser(
        prog=f"{PROG} web",
        description="启动本地只读面板（只监听 127.0.0.1，写操作请用 CLI 或 MCP）。",
    )
    parser.add_argument("-p", "--project", default=".", help="游戏项目根目录（默认当前目录）")
    parser.add_argument("--workdir", default=None, help="工作区目录（默认 <项目>/.gametrans）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            namespace = parser.parse_args(list(argv))
        except SystemExit as exc:
            return _exit_code(exc)

    app = WebApp(
        Path(namespace.project).expanduser(),
        # workdir 也归一：与项目根同一套口径，免得 `--workdir .` 这类写法在
        # 提取层那里变成空名字（见 core/session.py::_absolute）
        workdir=(
            Path(namespace.workdir).expanduser().resolve() if namespace.workdir else None
        ),
    )
    try:
        serve(app, DEFAULT_HOST, namespace.port, stream=out)
    except GameTransError as exc:
        print(f"错误：{exc.message}", file=err)
        if exc.hint:
            print(f"提示：{exc.hint}", file=err)
        return exc.exit_code
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout=None,
    stderr=None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    raw = list(sys.argv[1:] if argv is None else argv)

    # `gametrans mcp` / `gametrans web` 是传输通道而不是操作，所以不走操作注册表。
    command, position = _leading_command(raw)
    if command == "mcp":
        from gametrans.mcp_server import serve

        serve(sys.stdin, sys.stdout)
        return 0
    if command == "web":
        # 把命令 token 摘掉，剩下的（含它前面的全局 flag）交给子解析器
        return _run_web([*raw[:position], *raw[position + 1 :]], out, err)

    parser = build_parser()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            namespace = parser.parse_args(raw)
        except SystemExit as exc:
            return _exit_code(exc)

    operation: Operation | None = getattr(namespace, "operation", None)
    if operation is None:  # pragma: no cover - 注册表非空时不会发生
        print(parser.format_help(), file=out)
        return 2

    ctx = Context(
        # 归一成绝对路径：`--project` 默认就是当前目录（`.`），而提取层拿
        # `project_root.name` 当图上的根容器名 —— 留着相对路径会让根容器名为空，
        # 文件节点 id 与引擎骨架里的 `# game/...` 对不上，scan 直接崩。
        project_root=Path(namespace.project).expanduser().resolve(),
        workdir=(
            Path(namespace.workdir).expanduser().resolve() if namespace.workdir else None
        ),
        json_mode=bool(namespace.json),
        quiet=bool(namespace.quiet),
    )

    try:
        data = operation.handler(ctx, arguments_for(operation, vars(namespace)))
    except GameTransError as exc:
        return _report_error(err, ctx, exc)
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，绝不把裸 traceback 甩给用户
        if getattr(namespace, "debug", False):
            raise
        return _report_error(err, ctx, exc)

    if ctx.json_mode:
        print(json_dumps(result_envelope(data)), file=out)
    elif not ctx.quiet:
        session = ctx.peek()
        new_views = ctx.new_user_views()
        if new_views and session is not None:
            # 这次命令新产生的、且用户可见的信息 —— 不重播历史
            text = session.interaction.render(new_views)
        elif operation.render is not None:
            text = operation.render(data)
        else:
            text = _render_data(data)
        if text:
            print(text, file=out)
    return 0
