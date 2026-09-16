"""Command-line entry point; informational commands do not initialize Windows."""
import argparse
from pathlib import Path
import sys

from clipboard_typer import __version__
from clipboard_typer.core.config import read_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description="ClipboardTyper — Windows 剪贴板模拟输入器")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="启动托盘和图形界面（默认操作）")
    run.add_argument("--config", type=Path, help="使用指定的 JSON 配置文件")
    validate = commands.add_parser("validate-config", help="校验配置，不启动界面或注册快捷键")
    validate.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "validate-config":
        try:
            read_settings(args.path)
        except (OSError, ValueError) as exc:
            print(f"配置无效：{exc}", file=sys.stderr)
            return 1
        print(f"配置有效：{args.path}")
        return 0

    from clipboard_typer.gui import main as run_gui
    return run_gui(settings_path=getattr(args, "config", None))
