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

Windows 下双击 `build_exe.bat`。BAT 负责选择 Python 和创建 `.venv-build`，`scripts/build_exe.py` 负责依赖检测和 PyInstaller 打包。构建前退出已有 EXE，避免输出文件被占用。

```powershell
.\build_exe.bat                         # 默认单文件版，复用依赖和构建缓存
.\build_exe.bat --onedir                # 目录版，减少每次启动的解压开销
.\build_exe.bat --onedir --clean         # 目录版，强制重新分析
.\build_exe.bat --refresh-deps --no-pause # 重新安装依赖，完成后不等待按键
```

首次构建、`pyproject.toml` 内容变化、Python 或环境内已安装包的版本变化时，重新执行 `pip install -e ".[exe]"`。仅修改源码无需重新安装。成功安装后将指纹保存在 `.venv-build/.clipboard-typer-build.json`；失败不保留成功标记。`--refresh-deps` 强制重新执行安装，不代表升级所有依赖。缓存命中时检查构建工具和项目是否可导入，无需访问包源。

默认不传 `--clean`，由 PyInstaller 根据源码和资源变化更新缓存；单文件版和目录版分别使用 `build/onefile`、`build/onedir`，切换模式互不覆盖分析结果。`--clean` 清理 PyInstaller 缓存并重新分析，不重新安装依赖。

单文件版输出 `dist/ClipboardTyper.exe`；目录版输出 `dist/ClipboardTyper/ClipboardTyper.exe`，必须连同 `_internal` 和 `settings.json` 分发整个目录。两种模式保留各自 EXE 旁的配置，新建配置从项目根目录复制，不自动同步两个模式的配置。目录版先在 `build/onedir/staging` 构建，再覆盖复制程序文件，避免 PyInstaller 清空用户配置；输出目录中额外添加的文件也会保留。

CI 对 Windows/Linux、Python 3.9/3.13 运行 Ruff、Pytest、wheel 和源码包构建，并在安装 wheel 后从临时目录检查命令入口；独立 Windows 任务分别生成单文件版和目录版，上传构建产物供下载。CI 不自动发布到 PyPI 或部署。

## 从旧布局迁移

原 `clipboard_typer.py` 拆为 `core`、`platforms`、`services` 与入口；原 `clipboard_typer_ui.py` 拆为 `ui`。原使用说明迁至 `docs/usage.md`，根目录 `README.md` 作为项目入口。原 `tests/test_settings_ui.py` 迁至 `tests/integration/`，场景保留，导入和 mock 位置适配新模块。

启动方式改为 `python -m clipboard_typer` 或安装后的 `clipboard-typer-gui`。现有根目录与 EXE 旁的 `settings.json` 不需要迁移。普通包安装使用用户数据目录。
