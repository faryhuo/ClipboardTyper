"""Native Windows notification-area icon and menu."""
import ctypes as C
import time
import traceback
from clipboard_typer.core.events import UiEvent
from clipboard_typer.platforms.windows import NOTIFYICONDATAW, POINT, WM_HOTKEY, WNDCLASSW


class TrayIcon:
    CLASS_NAME = "PythonClipboardTyper.Tray.93ad601e"
    CALLBACK_MESSAGE = 0x8000 + 20
    STOP_COMMAND, EXIT_COMMAND, PAUSE_COMMAND, RESUME_COMMAND = 1001, 1002, 1003, 1004
    EDIT_COMMAND, RELOAD_COMMAND, LOG_COMMAND, ERROR_COMMAND, ACK_COMMAND = 1005, 1006, 1007, 1008, 1009
    SETTINGS_COMMAND, POPUP_COMMAND, FOLLOW_COMMAND = 1010, 1011, 1012

    def __init__(self, app):
        self.app, self.win = app, app.win
        self.hwnd, self.callback, self.module = None, None, None
        self.class_registered, self.added, self.version4 = False, False, False
        self.in_menu, self.next_retry, self.taskbar_created = False, 0, 0
        self.last_tip = None
        self.data = NOTIFYICONDATAW()
        self.data.cbSize = C.sizeof(self.data)

    def open(self):
        w = self.win
        self.module = w.GetModuleHandleW(None)
        self.taskbar_created = w.RegisterWindowMessageW("TaskbarCreated")
        if not self.taskbar_created:
            raise C.WinError(C.get_last_error())
        self.callback = w.WNDPROC(self.window_proc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = C.cast(self.callback, C.c_void_p).value
        wc.hInstance, wc.lpszClassName = self.module, self.CLASS_NAME
        if not w.RegisterClassW(C.byref(wc)):
            raise C.WinError(C.get_last_error())
        self.class_registered = True
        self.hwnd = w.CreateWindowExW(0x80, self.CLASS_NAME, "ClipboardTyper", 0,
                                      0, 0, 0, 0, None, None, self.module, None)
        if not self.hwnd:
            raise C.WinError(C.get_last_error())
        self.data.hWnd, self.data.uID = self.hwnd, 1
        self.data.uCallbackMessage = self.CALLBACK_MESSAGE
        self.data.hIcon = (w.LoadIconW(self.module, C.c_void_p(1))
                           or w.LoadIconW(None, C.c_void_p(32512)))
        if not self.data.hIcon:
            raise C.WinError(C.get_last_error())
        self.set_status("就绪；右键可查看进度、配置和日志")
        if not self.add_icon():
            raise RuntimeError("无法创建系统托盘图标，请确认 Windows 桌面已加载后重试")

    def add_icon(self):
        self.data.uFlags = 0x87
        self.added = bool(self.win.Shell_NotifyIconW(0, C.byref(self.data)))
        if self.added:
            self.data.uVersion = 4
            self.version4 = bool(self.win.Shell_NotifyIconW(4, C.byref(self.data)))
        return self.added

    def set_status(self, message):
        if message == self.last_tip:
            return
        self.last_tip = message
        raw = ("ClipboardTyper | " + message).encode("utf-16-le", errors="replace")[:254]
        raw = raw.decode("utf-16-le", errors="ignore").encode("utf-16-le")
        address = C.addressof(self.data) + NOTIFYICONDATAW.szTip.offset
        C.memset(address, 0, 256)
        C.memmove(address, raw, len(raw))
        if self.added:
            self.data.uFlags = 0x84
            self.win.Shell_NotifyIconW(1, C.byref(self.data))

    def window_proc(self, hwnd, message, wparam, lparam):
        try:
            if self.taskbar_created and message == self.taskbar_created:
                self.added, self.next_retry = False, 0
                return 0
            if message == WM_HOTKEY:
                self.app.on_hotkey(wparam)
                return 0
            if message == self.CALLBACK_MESSAGE:
                event = lparam & 0xFFFF if self.version4 else lparam
                if event == 0x0203:  # left double-click
                    self.app.open_settings()
                    return 0
                if ((self.version4 and event == 0x007B)
                        or (not self.version4 and event == 0x0205)):
                    self.show_menu()
                return 0
            if message == 0x0010 or (message == 0x0016 and wparam):
                self.app.quit()
                return 0
        except Exception as exc:
            self.app.notices.put(UiEvent("error", "托盘操作失败：" + str(exc), traceback.format_exc()))
        return self.win.DefWindowProcW(hwnd, message, wparam, lparam)

    def show_menu(self):
        if self.in_menu or self.app.shutdown.is_set():
            return
        self.in_menu = True
        w, menu, command = self.win, None, 0
        was_busy = self.app.busy()
        was_running = was_busy and not self.app.job.is_paused()
        if was_busy:
            self.app.pause("已打开托盘菜单")
        try:
            menu = w.CreatePopupMenu()
            if not menu:
                raise C.WinError(C.get_last_error())
            snapshot = self.app.job.snapshot() if self.app.job else None
            title = "ClipboardTyper — " + (snapshot["state"] if snapshot else "就绪")
            entries = [(2, 0, title)]
            if snapshot and snapshot["total"]:
                entries.append((2, 0, self.app.progress_text(snapshot)))
            entries.extend([
                (0x0800, 0, None),
                (0 if was_busy else 2, self.PAUSE_COMMAND, "暂停输入\t" + self.app.shortcut("pause_resume")),
                (0 if was_busy else 2, self.RESUME_COMMAND, "继续输入\t" + self.app.shortcut("pause_resume")),
                (0 if was_busy else 2, self.STOP_COMMAND, "中止输入\t" + self.app.shortcut("stop")),
                (0x0800, 0, None),
                (0, self.SETTINGS_COMMAND, "设置…"),
                (0, self.POPUP_COMMAND, "隐藏状态卡片" if self.app.flash.is_allowed() else "显示状态卡片"),
                (0, self.FOLLOW_COMMAND, "卡片跟随目标窗口所在屏幕"),
                (0, self.EDIT_COMMAND, "编辑 JSON 配置（高级）"),
                (2 if self.app.reloading else 0, self.RELOAD_COMMAND, "重新加载配置"),
                (0, self.LOG_COMMAND, "打开日志"),
                (0 if self.app.last_error else 2, self.ERROR_COMMAND, "查看最近错误"),
                (0 if self.app.error_sticky else 2, self.ACK_COMMAND, "确认错误"),
                (0x0800, 0, None),
                (0, self.EXIT_COMMAND, "退出程序\t" + self.app.shortcut("quit")),
            ])
            for flags, item_id, label in entries:
                if not w.AppendMenuW(menu, flags, item_id, label):
                    raise C.WinError(C.get_last_error())
            point = POINT()
            w.GetCursorPos(C.byref(point))
            w.SetForegroundWindow(self.hwnd)
            command = w.TrackPopupMenu(menu, 0x0182, point.x, point.y, 0, self.hwnd, None)
        finally:
            w.PostMessageW(self.hwnd, 0, 0, 0)
            if self.added:
                w.Shell_NotifyIconW(3, C.byref(self.data))
            if menu:
                w.DestroyMenu(menu)
            self.in_menu = False
        if self.app.shutdown.is_set():
            return
        if command == self.EXIT_COMMAND:
            self.app.quit()
        elif command == self.STOP_COMMAND:
            self.app.stop()
        elif command == self.PAUSE_COMMAND:
            self.app.pause()
        elif command == self.RESUME_COMMAND:
            self.app.resume(from_tray=True)
        elif command == self.SETTINGS_COMMAND:
            self.app.open_settings()
        elif command == self.POPUP_COMMAND:
            self.app.toggle_popup()
            if was_running and not self.app.flash.is_allowed():
                self.app.resume(from_tray=True)
        elif command == self.FOLLOW_COMMAND:
            self.app.flash.follow_target()
        elif command == self.EDIT_COMMAND:
            self.app.open_text_file(self.app.settings_path)
        elif command == self.RELOAD_COMMAND:
            self.app.reload_settings()
        elif command == self.LOG_COMMAND:
            self.app.open_text_file(self.app.log_path)
        elif command == self.ERROR_COMMAND:
            self.app.show_last_error()
        elif command == self.ACK_COMMAND:
            self.app.acknowledge_error()

    def tick(self):
        if not self.added and time.monotonic() >= self.next_retry:
            self.add_icon()
            self.next_retry = time.monotonic() + 2

    def close(self):
        if self.added:
            self.win.Shell_NotifyIconW(2, C.byref(self.data))
            self.added = False
        if self.hwnd:
            self.win.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.class_registered:
            self.win.UnregisterClassW(self.CLASS_NAME, self.module)
            self.class_registered = False
