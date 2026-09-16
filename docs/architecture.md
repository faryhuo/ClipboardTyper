# 架构说明

项目采用 `src/clipboard_typer` 布局。包安装后导入 `clipboard_typer`；测试通过安装包运行，避免根目录脚本遮蔽真实包。

| 模块 | 职责 |
| --- | --- |
| `core/config.py` | 默认值、快捷键解析、配置校验、原子 JSON 保存 |
| `core/paths.py` | 源码、安装包、EXE 的配置与日志位置 |
| `core/logger.py` | 队列日志、后台写入与文件轮转 |
| `core/events.py` | 跨线程事件数据 `UiEvent` |
| `platforms/windows.py` | ctypes ABI、Win32 API、输入事件编码、单实例、原生事件唤醒 |
| `services/hotkeys.py` | 快捷键预注册/提交/回滚、物理键状态 |
| `services/profiles.py` | 通用、RDP、Citrix 速度选择 |
| `services/typing_service.py` | 输入任务、暂停恢复、进度、窗口与修饰键保护 |
| `services/application.py` | 主消息循环及输入、保存、配置界面的协调 |
| `ui/status_card.py` | 不抢焦点的原生状态卡片、DPI 与位置处理 |
| `ui/settings.py` | Tk 设置表单及 `ConfigService` 界面线程桥接 |
| `ui/tray.py` | Windows 托盘和菜单 |
| `cli.py` / `gui.py` | 命令分发 / 桌面资源初始化和清理 |

`core` 不依赖界面或 Windows DLL。业务服务复用配置和平台接口；`application` 负责组装界面与服务。`ConfigService` 管理 Tk 生命周期，因此放在 `ui`，不属于配置持久化服务。所有模块导入时均不创建窗口、注册快捷键或加载 Windows DLL，只有桌面启动路径初始化原生资源。

原有线程边界保持不变：Windows 消息线程协调任务；输入和文件保存各有工作线程；Tk 对象只在 Tk 线程创建、使用和销毁；日志队列在独立线程写入。配置保存继续采用“预留快捷键 → 后台原子写入 → 提交或回滚”，防止保存失败时配置和快捷键状态不一致。

公开 API 为 `ConfigError`、`read_settings`、`validate_settings` 和 `__version__`。内部类按所在模块导入。运行时使用标准库 `argparse`、`dataclasses`、`logging` 和 `tkinter`；开发与打包依赖集中在 `pyproject.toml` 的可选依赖中。
