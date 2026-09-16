# ClipboardTyper

Windows 剪贴板模拟输入工具：通过全局快捷键逐字或分块输入文本，提供暂停/继续、焦点保护、远程桌面专用速度、托盘和图形设置。运行代码只依赖 Python 标准库。

## 安装与运行

桌面功能需要 Windows、Python 3.9+ 和 Tcl/Tk（官方 Python 安装器中的 **Tcl/Tk and IDLE**）。在项目目录运行：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m clipboard_typer
```

激活虚拟环境后也可使用以下入口：

```powershell
clipboard-typer                     # 启动托盘与 GUI
clipboard-typer-gui                 # 无控制台 GUI 入口
clipboard-typer run --config settings.json
clipboard-typer --version
clipboard-typer validate-config settings.json
```

普通安装使用 `python -m pip install .`，不需要开发依赖。帮助、版本、配置校验和配置 API 可在非 Windows 系统使用；模拟输入与 GUI 依赖 Windows API。

先复制文本，再点击目标输入位置。默认 `Ctrl+J` 慢速、`Ctrl+K` 快速、`F8` 暂停/继续、`Ctrl+Alt+S` 中止、`Ctrl+Alt+Q` 退出。双击托盘图标打开设置。

## 项目结构

```text
ClipboardTyper/
├── .github/workflows/ci.yml
├── docs/
│   ├── architecture.md
│   ├── development.md
│   └── usage.md
├── src/clipboard_typer/
│   ├── __init__.py          # 公开配置 API 与版本
│   ├── __main__.py          # python -m clipboard_typer
│   ├── core/               # 配置、路径、日志、事件数据
│   ├── platforms/          # Windows API 与原生资源
│   ├── services/           # 输入任务、快捷键、速度选择、应用协调
│   ├── ui/                 # 状态卡片、Tk 设置、托盘
│   ├── cli.py              # argparse 命令行入口
│   └── gui.py              # 桌面启动入口
├── scripts/run_gui.py      # PyInstaller 启动脚本
├── tests/
│   ├── conftest.py
│   ├── unit/
│   └── integration/
├── .dockerignore
├── .gitignore
├── build_exe.bat
├── install_pyinstaller.bat
├── settings.json
├── README.md
└── pyproject.toml
```

使用标准 `src/包名` 布局，`src` 本身不是导入包。旧的两个根目录脚本已拆入包内，源码启动改用安装后的 `python -m clipboard_typer`；无需再手动把 UI 文件放在脚本旁。

## 配置与日志

- 源码/可编辑安装默认读取项目根目录的 `settings.json`。
- EXE 默认读取 EXE 旁的 `settings.json`，重打包保留已有配置。
- 普通包安装使用 `%LOCALAPPDATA%\ClipboardTyper\settings.json`，不写入 `site-packages`。
- 可通过 `run --config PATH` 或 `CLIPBOARD_TYPER_CONFIG` 指定配置，命令行优先；相对路径按当前工作目录解析。
- 默认配置文件不存在时生成默认值；源码/EXE 目录无法创建文件时回退到用户数据目录。显式指定路径失败会报错，不会悄悄切换文件。
- 日志始终写入 `%LOCALAPPDATA%\ClipboardTyper\clipboard_typer.log`，后台轮转，不记录剪贴板正文。

现有 JSON 格式及旧配置补默认值的行为保留。详细操作、参数说明与限制见 [使用手册](docs/usage.md)。

## 开发与打包

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m build
.\build_exe.bat
```

`build` 生成 wheel 和源码包；`build_exe.bat` 创建独立的 `.venv-build` 并生成 `dist\ClipboardTyper.exe`。最终用户无需安装 Python。GitHub Actions 执行 Windows/Linux 检查、测试和 Python 包构建，并生成 Windows EXE 构建产物。

详见 [架构说明](docs/architecture.md) 与 [开发说明](docs/development.md)。
