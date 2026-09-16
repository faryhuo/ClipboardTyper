"""Desktop application entry point."""



def main(settings_path=None):
    import copy
    import ctypes as C
    import sys
    import traceback
    from clipboard_typer.core.config import DEFAULT_SETTINGS, read_settings
    from clipboard_typer.core.events import UiEvent
    from clipboard_typer.core.logger import close_logger, make_logger
    from clipboard_typer.core.paths import application_paths
    from clipboard_typer.platforms.windows import SingleInstance, Win32
    from clipboard_typer.services.application import App

    if sys.platform != "win32":
        print("此程序使用 Windows API，只支持 Windows")
        return 1
    # Match native card typography to the Windows display scale.
    try:
        C.WinDLL("user32").SetProcessDPIAware()
    except (AttributeError, OSError):
        pass
    win = Win32()
    instance, logger = SingleInstance(win), None
    try:
        instance.acquire()
        settings_path, log_path = application_paths(settings_path)
        logger = make_logger(log_path)
        initial_error = None
        try:
            settings = read_settings(settings_path)
        except Exception as exc:
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            initial_error = UiEvent("error", "配置读取失败，暂用默认配置；请从托盘编辑并重载：" + str(exc), traceback.format_exc())
        logger.info("Starting; settings=%s; log=%s", settings_path, log_path)
        App(win, instance, settings, settings_path, log_path, logger).run(initial_error)
    except Exception as exc:
        if logger:
            logger.exception("Startup or main-loop failure")
        win.MessageBoxW(None, str(exc), "ClipboardTyper 启动或运行失败", 0x10)
        return 1
    finally:
        instance.close()
        if logger:
            close_logger(logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
