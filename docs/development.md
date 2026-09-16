# 开发与验证

## 开发环境

使用 Python 3.9+，运行 `python -m pip install -e ".[dev]"`。Windows GUI 测试还要求 Tcl/Tk。`pyproject.toml` 统一管理包信息、构建、可选依赖、入口、Pytest 和 Ruff。

```powershell
python -m ruff check .
python -m pytest
python -m pytest tests/unit
python -m pytest tests/integration
python -m build
```

单元测试使用临时文件和模拟平台调用验证配置、原子写入失败、日志、快捷键事务及输入编码。集成测试验证安装后的命令入口、真实 Windows API 和 Tk 设置保存。Windows/Tk 场景在子进程内运行，以发现无法被 Python 异常捕获的原生崩溃；非 Windows 系统自动跳过此场景。

测试不注册实际全局快捷键，不向用户窗口发送按键。真实 Tk 窗口保持隐藏。RDP/Citrix 接收效果、混合 DPI 和显示器热插拔仍需手工验证。

## 构建

`python -m build` 输出 wheel 与源码包。可在另一虚拟环境安装生成的 wheel，然后从项目外运行 `python -m clipboard_typer --version` 和 `python -m clipboard_typer validate-config PATH`，验证安装完整性。

Windows 下双击 `build_exe.bat`。脚本检查 Tcl/Tk，使用 `scripts/run_gui.py` 和 `--paths src` 打包，保留已有 `dist/settings.json`。打包依赖来自 `.[exe]`。构建前退出已有 EXE，避免输出文件被占用。

CI 对 Windows/Linux、Python 3.9/3.13 运行 Ruff、Pytest、wheel 和源码包构建，并在安装 wheel 后从临时目录检查命令入口；独立 Windows 任务生成 EXE，上传构建产物供下载。CI 不自动发布到 PyPI 或部署。

## 从旧布局迁移

原 `clipboard_typer.py` 拆为 `core`、`platforms`、`services` 与入口；原 `clipboard_typer_ui.py` 拆为 `ui`。原使用说明迁至 `docs/usage.md`，根目录 `README.md` 作为项目入口。原 `tests/test_settings_ui.py` 迁至 `tests/integration/`，场景保留，导入和 mock 位置适配新模块。

启动方式改为 `python -m clipboard_typer` 或安装后的 `clipboard-typer-gui`。现有根目录与 EXE 旁的 `settings.json` 不需要迁移。普通包安装使用用户数据目录。
