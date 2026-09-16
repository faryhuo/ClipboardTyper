"""Presentation layer: native non-activating status card and a Tk settings window.

Tk is created, used and destroyed on its own UI thread. It drains a Python
queue from a Tk timer; native Windows callbacks must never re-enter Tk.
"""
import copy
import ctypes as C
import gc
import queue
import threading
import time
import traceback
from contextlib import contextmanager

UINT = DWORD = C.c_uint32
LONG = BOOL = C.c_int32
HANDLE = C.c_void_p
LRESULT = C.c_ssize_t
WPARAM = C.c_size_t
LPARAM = C.c_ssize_t


class RECT(C.Structure):
    _fields_ = [(name, LONG) for name in ("left", "top", "right", "bottom")]


class POINT(C.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class MONITORINFO(C.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", DWORD)]


def optional_bind(dll, name, result, *args):
    try:
        return bind(dll, name, result, *args)
    except AttributeError:
        return None


def clamp_card(area, width, height, x, y):
    return (max(area.left, min(x, max(area.left, area.right - width))),
            max(area.top, min(y, max(area.top, area.bottom - height))))


class PAINTSTRUCT(C.Structure):
    _fields_ = [("hdc", HANDLE), ("fErase", BOOL), ("rcPaint", RECT),
                ("fRestore", BOOL), ("fIncUpdate", BOOL), ("rgbReserved", C.c_byte * 32)]


class WNDCLASS(C.Structure):
    _fields_ = [("style", UINT), ("lpfnWndProc", C.c_void_p), ("cbClsExtra", C.c_int),
                ("cbWndExtra", C.c_int), ("hInstance", HANDLE), ("hIcon", HANDLE),
                ("hCursor", HANDLE), ("hbrBackground", HANDLE),
                ("lpszMenuName", C.c_wchar_p), ("lpszClassName", C.c_wchar_p)]


def bind(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, list(args)
    return fn


def rgb(value):
    value = value.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return r | (g << 8) | (b << 16)


def card_scene(message, snapshot=None, error=False, pause_key="F8"):
    """Shared logical layout for Windows painting and deterministic layout checks."""
    snapshot = snapshot or {}
    state = snapshot.get("state", "就绪")
    accent, tint = "#4169E1", "#EEF3FF"
    if error:
        title, badge, accent, tint = "需要处理", "错误", "#D14C57", "#FFF0F1"
    elif "暂停" in state or "等待松开" in state:
        title, badge, accent, tint = "输入已暂停", snapshot.get("label", "暂停"), "#B77B19", "#FFF7E7"
    elif state == "已完成":
        title, badge, accent, tint = "输入完成", snapshot.get("label", "完成"), "#168569", "#EAF8F2"
    elif state in ("正在输入", "准备输入"):
        title, badge = "正在输入" if state == "正在输入" else "准备输入", snapshot.get("label", "输入")
    elif state in ("已中止", "无文本"):
        title, badge, accent, tint = state, "已停止", "#64748B", "#F0F3F7"
    else:
        title, badge = "准备就绪", "READY"
    percent = max(0.0, min(100.0, snapshot.get("percent", 0)))
    total = snapshot.get("total", 0)
    progress = bool(total) and not error
    detail = message.split("\n", 1)[0].replace("错误：", "", 1)
    if progress:
        detail = f"已发送 {snapshot['sent']:,} / {total:,} 个字符"
    ops = [
        ("round", (0, 0, 380, 190), "#E0E6F0", 18),
        ("round", (1, 1, 379, 189), "#FFFFFF", 17),
        ("round", (18, 16, 48, 46), tint, 10),
        ("text", (18, 18, 48, 44), "C", accent, 17, True, "center"),
        ("text", (58, 17, 244, 44), "ClipboardTyper · Citrix" if snapshot.get("profile_name") == "Citrix Workspace" else "ClipboardTyper · RDP" if snapshot.get("profile_name") == "远程桌面" else "ClipboardTyper", "#546178", 12, True, "left"),
        ("text", (302, 13, 330, 44), "···", "#68768C", 18, True, "center"),
        ("text", (342, 14, 367, 43), "×", "#8793A5", 18, False, "center"),
        ("text", (20, 56, 210, 88), title, "#162238", 23, True, "left"),
        ("round", (302, 61, 360, 84), tint, 9),
        ("text", (302, 61, 360, 83), badge, accent, 11, True, "center"),
        ("text", (20, 96, 280 if progress else 360, 119 if progress else 143),
         detail, "#65738A", 12, False, "left" if progress else "wrap"),
    ]
    if progress:
        ops.extend([
            ("text", (279, 94, 360, 120), f"{percent:.1f}%", accent, 16, True, "right"),
            ("round", (20, 130, 360, 136), "#EDF1F7", 3),
        ])
        if percent > 0:
            ops.append(("round", (20, 130, 20 + max(3, 340 * percent / 100), 136), accent, 3))
        footer = f"第 {snapshot['line']}/{snapshot['lines']} 行  ·  剩余 {snapshot['remaining']:,}"
        hint = f"{pause_key} {'继续' if '暂停' in state or '等待松开' in state else '暂停'}"
        if state in ("已完成", "已中止", "无文本"):
            hint = "右键更多操作"
    else:
        footer = "右键查看错误与日志" if error else f"{pause_key} 暂停 / 继续"
        hint = "右键可隐藏"
    ops.extend([
        ("text", (20, 153, 253, 177), footer, "#8994A5", 11, False, "left"),
        ("text", (253, 153, 360, 177), hint, "#8994A5", 11, False, "right"),
    ])
    return ops


class StatusCard:
    CLASS_NAME = "ClipboardTyper.StatusCard.v2"
    RELAYOUT = 0x8000 + 72

    def __init__(self, win, enabled=True, menu_callback=None, hide_callback=None, error_callback=None):
        self.win, self.enabled = win, enabled
        self.error_callback, self.faulted, self.fault_detail = error_callback, False, ""
        self.menu_callback, self.hide_callback = menu_callback, hide_callback
        self.user_hidden, self.deadline, self.current = False, 0, None
        self.snapshot, self.error, self.pause_key = None, False, "F8"
        self.hwnd, self.registered = None, False
        self.fonts = {}
        self.region_size = None
        self.anchor, self.visible, self.manual_position = None, False, False
        self.placing, self.layout_pending, self.drag_origin = False, False, None
        self.event_hooks, self.event_callback = [], None
        u, g = win.user, win.gdi
        self.BeginPaint = bind(u, "BeginPaint", HANDLE, HANDLE, C.POINTER(PAINTSTRUCT))
        self.EndPaint = bind(u, "EndPaint", BOOL, HANDLE, C.POINTER(PAINTSTRUCT))
        self.InvalidateRect = bind(u, "InvalidateRect", BOOL, HANDLE, C.POINTER(RECT), BOOL)
        self.FillRect = bind(u, "FillRect", C.c_int, HANDLE, C.POINTER(RECT), HANDLE)
        self.DrawText = bind(u, "DrawTextW", C.c_int, HANDLE, C.c_wchar_p, C.c_int, C.POINTER(RECT), UINT)
        self.SetWindowRgn = bind(u, "SetWindowRgn", C.c_int, HANDLE, HANDLE, BOOL)
        self.LoadCursor = bind(u, "LoadCursorW", HANDLE, HANDLE, C.c_void_p)
        self.MonitorFromWindow = bind(u, "MonitorFromWindow", HANDLE, HANDLE, DWORD)
        self.GetMonitorInfo = bind(u, "GetMonitorInfoW", BOOL, HANDLE, C.POINTER(MONITORINFO))
        self.GetWindowRect = bind(u, "GetWindowRect", BOOL, HANDLE, C.POINTER(RECT))
        self.CursorPos = bind(u, "GetCursorPos", BOOL, C.c_void_p)
        self.SetCapture = bind(u, "SetCapture", HANDLE, HANDLE)
        self.ReleaseCapture = bind(u, "ReleaseCapture", BOOL)
        self.GetDpiForWindow = optional_bind(u, "GetDpiForWindow", UINT, HANDLE)
        self.DpiContext = optional_bind(u, "SetThreadDpiAwarenessContext", HANDLE, HANDLE)
        self.CreateFont = bind(g, "CreateFontW", HANDLE, *([C.c_int] * 5), *([DWORD] * 8), C.c_wchar_p)
        self.Brush = bind(g, "CreateSolidBrush", HANDLE, DWORD)
        self.Pen = bind(g, "CreatePen", HANDLE, C.c_int, C.c_int, DWORD)
        self.Select = bind(g, "SelectObject", HANDLE, HANDLE, HANDLE)
        self.Delete = bind(g, "DeleteObject", BOOL, HANDLE)
        self.RoundRect = bind(g, "RoundRect", BOOL, HANDLE, *([C.c_int] * 6))
        self.CreateRegion = bind(g, "CreateRoundRectRgn", HANDLE, *([C.c_int] * 6))
        self.TextColor = bind(g, "SetTextColor", DWORD, HANDLE, DWORD)
        self.BkMode = bind(g, "SetBkMode", C.c_int, HANDLE, C.c_int)
        self.CreateDC = bind(g, "CreateCompatibleDC", HANDLE, HANDLE)
        self.DeleteDC = bind(g, "DeleteDC", BOOL, HANDLE)
        self.CreateBitmap = bind(g, "CreateCompatibleBitmap", HANDLE, HANDLE, C.c_int, C.c_int)
        self.BitBlt = bind(g, "BitBlt", BOOL, HANDLE, C.c_int, C.c_int, C.c_int, C.c_int,
                           HANDLE, C.c_int, C.c_int, DWORD)
        get_dpi = getattr(u, "GetDpiForSystem", None)
        if get_dpi:
            get_dpi.restype, get_dpi.argtypes = UINT, []
        self.dpi = (get_dpi() if get_dpi else 96) or 96
        self.scale = self.dpi / 96
        self.width, self.height = self.px(380), self.px(190)
        self.callback = win.WNDPROC(self.window_proc)
        self.module = win.GetModuleHandleW(None)
        wc = WNDCLASS()
        wc.style = 0x20003  # CS_DROPSHADOW | CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = C.cast(self.callback, C.c_void_p).value
        wc.hInstance, wc.lpszClassName = self.module, self.CLASS_NAME
        wc.hCursor = self.LoadCursor(None, C.c_void_p(32512))
        if not win.RegisterClassW(C.cast(C.byref(wc), win.RegisterClassW.argtypes[0])):
            raise C.WinError(C.get_last_error())
        self.registered = True
        try:
            with self.dpi_scope():
                self.hwnd = win.CreateWindowExW(0x08000088, self.CLASS_NAME, "ClipboardTyper 状态",
                                                0x80000000, 0, 0, self.width, self.height,
                                                None, None, self.module, None)
            if not self.hwnd:
                raise C.WinError(C.get_last_error())
            self.rebuild_geometry(self.dpi)
            self.watch_windows()
        except Exception:
            self.close()
            raise

    @contextmanager
    def dpi_scope(self):
        previous = None
        if self.DpiContext:
            previous = self.DpiContext(HANDLE(-4)) or self.DpiContext(HANDLE(-3))
        try:
            yield
        finally:
            if previous:
                self.DpiContext(previous)

    def monitor_area(self, window=None):
        info = MONITORINFO()
        info.cbSize = C.sizeof(info)
        monitor = self.MonitorFromWindow(window or self.hwnd, 2)
        if monitor and self.GetMonitorInfo(monitor, C.byref(info)):
            return info.rcWork
        return RECT(0, 0, 1280, 720)

    def rebuild_geometry(self, dpi, area=None):
        self.dpi = max(48, min(int(dpi or 96), 768))
        scale = self.dpi / 96
        if area:
            scale = min(scale, max(1, area.right - area.left - 16) / 380,
                        max(1, area.bottom - area.top - 16) / 190)
        if abs(self.scale - scale) > 0.001:
            self.scale = scale
            for font in self.fonts.values():
                if font:
                    self.Delete(font)
            self.fonts.clear()
        self.width, self.height = self.px(380), self.px(190)
        if self.hwnd and self.region_size != (self.width, self.height):
            region = self.CreateRegion(0, 0, self.width + 1, self.height + 1, self.px(36), self.px(36))
            if region:
                if self.SetWindowRgn(self.hwnd, region, False):
                    self.region_size = (self.width, self.height)
                else:
                    self.Delete(region)

    def set_anchor(self, hwnd):
        self.anchor = hwnd
        self.schedule_layout()

    def follow_target(self):
        self.manual_position = False
        self.schedule_layout()

    def schedule_layout(self):
        if self.hwnd and self.visible and not self.layout_pending:
            self.layout_pending = True
            self.win.PostMessageW(self.hwnd, self.RELAYOUT, 0, 0)

    def watch_windows(self):
        self.WINEVENTPROC = getattr(C, "WINFUNCTYPE", C.CFUNCTYPE)(None, HANDLE, DWORD, HANDLE, LONG, LONG, DWORD, DWORD)
        self.event_callback = self.WINEVENTPROC(self.window_event)
        self.SetWinEventHook = bind(self.win.user, "SetWinEventHook", HANDLE,
                                    DWORD, DWORD, HANDLE, self.WINEVENTPROC, DWORD, DWORD, DWORD)
        self.UnhookWinEvent = bind(self.win.user, "UnhookWinEvent", BOOL, HANDLE)
        for event in (0x0003, 0x000B, 0x800B):  # foreground, move/size end, location change
            hook = self.SetWinEventHook(event, event, None, self.event_callback, 0, 0, 2)
            if hook:
                self.event_hooks.append(hook)

    def window_event(self, hook, event, hwnd, object_id, child_id, thread_id, stamp):
        try:
            self.handle_window_event(event, hwnd, object_id)
        except Exception:
            self.report_fault("屏幕位置更新失败")

    def handle_window_event(self, event, hwnd, object_id):
        if not self.visible or self.manual_position:
            return
        if self.anchor and self.win.IsWindow(self.anchor):
            relevant = hwnd == self.anchor and event in (0x000B, 0x800B) and object_id == 0
        else:
            relevant = event == 0x0003
        if relevant:
            self.schedule_layout()

    def place(self, show=False):
        if self.placing or self.drag_origin or not self.hwnd:
            return
        self.placing = True
        try:
            with self.dpi_scope():
                anchor = self.anchor if self.anchor and self.win.IsWindow(self.anchor) else self.win.GetForegroundWindow()
                area = self.monitor_area(self.hwnd if self.manual_position else anchor)
                # Moving the window to another monitor triggers WM_DPICHANGED.
                # Re-read OUR window's DPI; the target can itself be DPI-unaware.
                for _ in range(2):
                    self.rebuild_geometry(self.dpi, area)
                    if self.manual_position:
                        rect = RECT()
                        self.GetWindowRect(self.hwnd, C.byref(rect))
                        x, y = clamp_card(area, self.width, self.height, rect.left, rect.top)
                    else:
                        margin = self.px(24)
                        x, y = clamp_card(area, self.width, self.height,
                                          area.right - self.width - margin, area.bottom - self.height - margin)
                    self.win.SetWindowPos(self.hwnd, HANDLE(-1), x, y, self.width, self.height,
                                          0x0010 | (0x0040 if show else 0))
                    dpi = self.GetDpiForWindow(self.hwnd) if self.GetDpiForWindow else self.dpi
                    if dpi:
                        self.dpi = dpi
                self.InvalidateRect(self.hwnd, None, False)
        finally:
            self.placing = False

    def drag_start(self):
        with self.dpi_scope():
            point, rect = POINT(), RECT()
            if self.CursorPos(C.byref(point)) and self.GetWindowRect(self.hwnd, C.byref(rect)):
                self.manual_position = True
                self.drag_origin = (point.x, point.y, rect.left, rect.top)
                self.SetCapture(self.hwnd)

    def drag_move(self):
        if not self.drag_origin:
            return
        with self.dpi_scope():
            point = POINT()
            self.CursorPos(C.byref(point))
            ox, oy, left, top = self.drag_origin
            self.win.SetWindowPos(self.hwnd, HANDLE(-1), left + point.x - ox, top + point.y - oy,
                                  self.width, self.height, 0x0010)

    def drag_end(self):
        if self.drag_origin:
            self.drag_origin = None
            self.ReleaseCapture()
            self.place()


    def px(self, value):
        return round(value * self.scale)

    def set_context(self, snapshot=None, error=False, pause_key="F8"):
        self.snapshot, self.error, self.pause_key = snapshot, error, pause_key

    def is_allowed(self):
        return self.enabled and not self.user_hidden and not self.faulted

    def report_fault(self, operation):
        # Latch before hiding: native callbacks can re-enter while hiding a window.
        if self.faulted:
            return
        self.faulted = True
        self.fault_detail = traceback.format_exc()
        self.visible, self.deadline, self.drag_origin = False, 0, None
        try:
            self.ReleaseCapture()
            if self.hwnd:
                self.win.ShowWindow(self.hwnd, 0)
        except Exception:
            self.fault_detail += "\n隐藏卡片也失败：\n" + traceback.format_exc()
        if self.error_callback:
            self.error_callback("状态卡片" + operation + "；已隐藏，可从托盘查看错误并重新显示", self.fault_detail)

    def show(self, message, milliseconds=4000, persistent=False):
        self.current = message
        if not self.is_allowed() or not self.hwnd:
            return
        try:
            self.visible = True
            self.place(show=True)
            if not self.faulted:
                self.deadline = 0 if persistent else time.monotonic() + milliseconds / 1000
        except Exception:
            self.report_fault("显示失败")

    def hide(self):
        self.visible = False
        self.deadline = 0
        try:
            self.drag_end()
            if self.hwnd:
                self.win.ShowWindow(self.hwnd, 0)
        except Exception:
            self.report_fault("隐藏失败")

    def hide_by_user(self):
        self.user_hidden = True
        self.hide()

    def restore(self):
        self.user_hidden, self.enabled = False, True
        self.faulted = False

    def set_enabled(self, enabled):
        self.enabled = enabled
        if not enabled:
            self.hide()

    def tick(self):
        if self.deadline and time.monotonic() >= self.deadline:
            self.hide()

    def window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == self.RELAYOUT:
                self.layout_pending = False
                if self.visible and self.is_allowed():
                    self.place()
                return 0
            if message == 0x02E0:  # WM_DPICHANGED
                self.rebuild_geometry(wparam & 0xFFFF)
                self.schedule_layout()
                return 0
            if message in (0x007E, 0x001A):  # display / work-area changes
                self.schedule_layout()
            if message == 0x0201:  # title drag, excluding menu and hide buttons
                x, y = C.c_int16(lparam & 0xFFFF).value, C.c_int16((lparam >> 16) & 0xFFFF).value
                if 0 <= y < self.px(50) and 0 <= x < self.px(292):
                    self.drag_start()
                    return 0
            if message == 0x0200 and self.drag_origin:
                self.drag_move()
                return 0
            if message == 0x0215:  # capture lost
                self.drag_origin = None
                self.schedule_layout()
                return 0
            if message == 0x000F:  # WM_PAINT
                self.paint(hwnd)
                return 0
            if message == 0x0014:  # WM_ERASEBKGND: double buffered
                return 1
            if message == 0x0021:  # WM_MOUSEACTIVATE / MA_NOACTIVATE
                return 3
            if message == 0x007B:  # WM_CONTEXTMENU
                if self.menu_callback:
                    self.menu_callback()
                return 0
            if message == 0x0202:  # WM_LBUTTONUP
                if self.drag_origin:
                    self.drag_end()
                    return 0
                x, y = lparam & 0xFFFF, (lparam >> 16) & 0xFFFF
                if y < self.px(50) and x >= self.px(337):
                    if self.hide_callback:
                        self.hide_callback()
                elif y < self.px(50) and x >= self.px(292) and self.menu_callback:
                    self.menu_callback()
                return 0
        except Exception:
            # Never let a Python exception cross a native window-procedure boundary.
            self.report_fault("窗口操作失败")
        return self.win.DefWindowProcW(hwnd, message, wparam, lparam)

    def paint(self, hwnd):
        ps = PAINTSTRUCT()
        screen = self.BeginPaint(hwnd, C.byref(ps))
        dc, bitmap, previous = None, None, None
        try:
            dc = self.CreateDC(screen)
            bitmap = self.CreateBitmap(screen, self.width, self.height) if dc else None
            if not screen or not dc or not bitmap:
                raise RuntimeError("无法分配状态卡片的绘图缓冲区")
            previous = self.Select(dc, bitmap)
            self.BkMode(dc, 1)  # TRANSPARENT
            background = self.Brush(rgb("#FFFFFF"))
            bounds = RECT(0, 0, self.width, self.height)
            self.FillRect(dc, C.byref(bounds), background)
            self.Delete(background)
            for op in card_scene(self.current or "", self.snapshot, self.error, self.pause_key):
                rect = RECT(*(self.px(n) for n in op[1]))
                if op[0] == "round":
                    color, radius = op[2:]
                    brush, pen = self.Brush(rgb(color)), self.Pen(0, 1, rgb(color))
                    old_brush, old_pen = self.Select(dc, brush), self.Select(dc, pen)
                    try:
                        self.RoundRect(dc, rect.left, rect.top, rect.right, rect.bottom,
                                       self.px(radius * 2), self.px(radius * 2))
                    finally:
                        self.Select(dc, old_brush)
                        self.Select(dc, old_pen)
                        self.Delete(brush)
                        self.Delete(pen)
                else:
                    text, color, size, bold, alignment = op[2:]
                    key = size, bold
                    if key not in self.fonts:
                        self.fonts[key] = self.CreateFont(-self.px(size), 0, 0, 0, 600 if bold else 400,
                                                         0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei UI")
                    previous_font = self.Select(dc, self.fonts[key])
                    self.TextColor(dc, rgb(color))
                    flags = 0x0800 | 0x8000  # DT_NOPREFIX | DT_END_ELLIPSIS
                    if alignment == "wrap":
                        flags |= 0x10
                    else:
                        flags |= 0x24 | {"left": 0, "center": 1, "right": 2}[alignment]
                    self.DrawText(dc, text, -1, C.byref(rect), flags)
                    self.Select(dc, previous_font)
            self.BitBlt(screen, 0, 0, self.width, self.height, dc, 0, 0, 0x00CC0020)
        finally:
            if previous:
                self.Select(dc, previous)
            if bitmap:
                self.Delete(bitmap)
            if dc:
                self.DeleteDC(dc)
            self.EndPaint(hwnd, C.byref(ps))

    def close(self):
        for hook in self.event_hooks:
            self.UnhookWinEvent(hook)
        self.event_hooks.clear()
        if self.hwnd:
            self.win.DestroyWindow(self.hwnd)
            self.hwnd = None
        for font in self.fonts.values():
            if font:
                self.Delete(font)
        self.fonts.clear()
        if self.registered:
            self.win.UnregisterClassW(self.CLASS_NAME, self.module)
            self.registered = False


PROFILE_FIELDS = [
    ("keyDelay", "每字延迟", "ms；0 / -1 为整块发送", -1, 60000),
    ("chunk", "每块字符数", "越大越快，暂停粒度越粗", 1, 256),
    ("pause", "块间停顿", "ms；掉字时可适当增大", 0, 60000),
    ("breatherEvery", "长停顿间隔", "块；0 为关闭", 0, 1000000),
    ("breatherPause", "长停顿时长", "ms", 0, 60000),
    ("linePause", "换行停顿", "ms", 0, 60000),
]
HOTKEY_FIELDS = [("slow", "慢速输入"), ("fast", "快速输入"),
                 ("pause_resume", "暂停 / 继续"), ("stop", "中止输入"), ("quit", "退出程序")]
BOOL_FIELDS = [
    ("show_popup", "显示状态卡片", "启动后显示右下角的小卡片，可从右键菜单临时隐藏。"),
    ("show_progress", "显示输入进度", "显示字符数量、百分比、行号和剩余量。"),
    ("stop_on_focus_loss", "保护原输入窗口", "关闭不会变成后台输入；内容可能被发往新焦点窗口。"),
    ("pause_on_focus_loss", "切换窗口时暂停", "关闭后，开启窗口保护时将直接中止任务。"),
    ("pause_on_modifiers", "手动按修饰键时暂停", "Ctrl / Alt / Shift / Win；松开后手动继续。"),
    ("clear_auto_indent", "换行后清除自动缩进", "发送 Shift+Home 和 Delete；不需要时可关闭。"),
]
NUMERIC_FIELDS = [("start_delay_ms", "启动等待", 0, 60000),
                  ("progress_interval_ms", "进度更新间隔", 100, 5000),
                  ("notice_duration_ms", "普通提示时长", 1000, 60000)]


class ConfigService:
    """Thread-safe facade. Only plain data crosses threads; Tk objects do not."""
    QUEUE_INTERVAL_MS = 50

    def __init__(self, win, notify, defaults, validator):
        self.win, self.notify, self.defaults, self.validator = win, notify, defaults, validator
        self.queue, self.lock = queue.SimpleQueue(), threading.Lock()
        self.thread = None
        self.closing = False

    def post(self, kind, data=None):
        with self.lock:
            if self.closing and kind != "quit":
                return
            self.queue.put((kind, data))

    def open(self, settings, path):
        self.post("open", (copy.deepcopy(settings), str(path)))
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.run, name="settings-ui", daemon=True)
                self.thread.start()

    def close(self):
        with self.lock:
            self.closing = True
        self.post("quit")
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)

    def run(self):
        root, editor = None, None
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            ancestor = bind(self.win.user, "GetAncestor", HANDLE, HANDLE, UINT)
            editor = SettingsEditor(root, self.defaults, self.validator, self.notify,
                                    window_handle=lambda: ancestor(root.winfo_id(), 2))
            def drain():
                # Even on this thread, calling Tk from a ctypes WNDPROC can
                # invalidate _tkinter's saved thread state inside mainloop.
                # Schedule exclusively from Tk callbacks, never native ones.
                # Schedule first so a recoverable UI error cannot stop delivery.
                root.after(self.QUEUE_INTERVAL_MS, drain)
                while True:
                    try:
                        kind, data = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "open":
                        editor.open(*data)
                    elif kind == "result":
                        editor.save_result(data)
                    elif kind == "record":
                        editor.record_result(data)
                    elif kind == "quit":
                        root.quit()
                        return
            root.after(0, drain)
            root.mainloop()
        except Exception as exc:
            self.notify("gui_failed", "无法打开设置窗口：" + str(exc), traceback.format_exc())
        finally:
            if root:
                try:
                    root.destroy()
                except Exception:
                    pass
            # Dispose Tcl-owned variables on their creator thread as well.
            editor, root = None, None
            gc.collect()


class SettingsEditor:
    BG, INK, MUTED, BLUE = "#F5F7FB", "#162238", "#778399", "#4169E1"

    def __init__(self, root, defaults, validator, notify, window_handle=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root, self.defaults, self.validator, self.notify = root, defaults, validator, notify
        self.variables, self.inputs, self.integer_labels = {}, {}, {}
        self.settings, self.path, self.saving, self.visible = None, "", False, False
        self.window_handle = window_handle or root.winfo_id
        self.recording, self.record_token, self.record_timer = None, 0, None
        self.record_buttons = {}
        root.title("ClipboardTyper · 设置")
        root.configure(bg=self.BG)
        scale = max(1.0, root.winfo_fpixels("1i") / 96)
        screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
        width = min(round(780 * scale), max(620, screen_width - 80))
        height = min(round(700 * scale), max(480, screen_height - 100))
        root.geometry(f"{width}x{height}+{max(0, (screen_width-width)//2)}+{max(0, (screen_height-height)//2)}")
        root.minsize(min(width, round(700 * scale)), min(height, round(600 * scale)))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.callback_error
        # Microsoft YaHei UI is supplied by Windows; Tk substitutes on other platforms.
        self.font = "Microsoft YaHei UI"
        root.option_add("*Font", (self.font, 10))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background=self.BG)
        style.configure("White.TFrame", background="#FFFFFF")
        style.configure("TLabel", background=self.BG, foreground=self.INK)
        style.configure("TNotebook", background=self.BG, borderwidth=0, tabmargins=(0, 0, 0, 12))
        style.configure("TNotebook.Tab", padding=(22, 11), background="#EAF0F8", foreground=self.MUTED, borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", "#FFFFFF")], foreground=[("selected", self.BLUE)])
        style.configure("TEntry", padding=7, fieldbackground="#FFFFFF", bordercolor="#DCE3EF", lightcolor="#DCE3EF", darkcolor="#DCE3EF")
        style.configure("TSpinbox", padding=6, fieldbackground="#FFFFFF", bordercolor="#DCE3EF")
        style.configure("TCheckbutton", background="#FFFFFF", foreground=self.INK, padding=(0, 3))
        style.map("TCheckbutton", background=[("active", "#FFFFFF")])
        style.configure("TButton", padding=(15, 9), background="#E9EEF7", foreground=self.INK, borderwidth=0)
        style.map("TButton", background=[("active", "#DDE6F4")])
        style.configure("Primary.TButton", background=self.BLUE, foreground="#FFFFFF")
        style.map("Primary.TButton", background=[("disabled", "#ADBCE8"), ("active", "#3458C5")], foreground=[("disabled", "#FFFFFF")])
        header = tk.Frame(root, bg=self.BG)
        header.pack(fill="x", padx=30, pady=(24, 18))
        logo = tk.Label(header, text=" C ", bg=self.BLUE, fg="#FFFFFF", font=(self.font, 19, "bold"), padx=7, pady=4)
        logo.pack(side="left", padx=(0, 14))
        title = tk.Frame(header, bg=self.BG)
        title.pack(side="left")
        tk.Label(title, text="ClipboardTyper", bg=self.BG, fg=self.INK, font=(self.font, 20, "bold")).pack(anchor="w")
        tk.Label(title, text="按你的节奏输入，每个选项都可以在这里调整。", bg=self.BG, fg=self.MUTED, font=(self.font, 10)).pack(anchor="w", pady=(3, 0))
        # Bottom controls are packed first, so they stay visible when resized.
        footer = tk.Frame(root, bg=self.BG)
        footer.pack(side="bottom", fill="x", padx=30, pady=(10, 20))
        self.status = tk.Label(footer, text="", bg=self.BG, fg=self.MUTED, anchor="w", justify="left", wraplength=width-80)
        self.status.pack(fill="x", pady=(0, 10))
        self.location = tk.Label(footer, text="", bg=self.BG, fg=self.MUTED, font=(self.font, 9), anchor="w", wraplength=width-80, justify="left")
        self.location.pack(fill="x", pady=(0, 10))
        buttons = tk.Frame(footer, bg=self.BG)
        buttons.pack(fill="x")
        self.reset_button = ttk.Button(buttons, text="恢复默认值", command=self.reset)
        self.reset_button.pack(side="left")
        self.save_button = ttk.Button(buttons, text="保存并应用", style="Primary.TButton", command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(buttons, text="关闭", command=self.close).pack(side="right", padx=(0, 10))
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=30)
        speed = self.page("通用速度")
        remote = self.page("远程桌面")
        shortcuts = self.page("快捷键")
        options = self.page("显示与保护")
        self.build_speed(speed)
        self.build_remote(remote)
        self.build_hotkeys(shortcuts)
        self.build_options(options)
        root.bind("<Control-s>", lambda event: self.save())
        root.bind("<FocusOut>", lambda event: root.after_idle(self.record_focus_check), add="+")

    def page(self, label):
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.tabs, style="White.TFrame")
        self.tabs.add(outer, text=label)
        canvas = tk.Canvas(outer, bg="#FFFFFF", highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        bar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        canvas.configure(yscrollcommand=bar.set)
        body = tk.Frame(canvas, bg="#FFFFFF", padx=20, pady=16)
        item = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        # Scrolling belongs to the visible page, not a process-wide bind_all.
        self.root.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units")
                       if self.tabs.select() == str(outer) else None, add="+")
        return body

    def label(self, parent, text, muted=False, **kwargs):
        return self.tk.Label(parent, text=text, bg="#FFFFFF", fg=self.MUTED if muted else self.INK,
                             font=(self.font, 9 if muted else 10), **kwargs)

    def spin(self, parent, path, label, lo, hi, width=10):
        variable = self.tk.StringVar(master=self.root)
        widget = self.ttk.Spinbox(parent, from_=lo, to=hi, textvariable=variable, width=width)
        self.variables[path], self.inputs[path] = variable, widget
        self.integer_labels[path] = label
        return widget

    def build_speed(self, page, prefix=("profiles",)):
        self.label(page, "快慢两套参数，单位为毫秒；保存后从下一次输入任务生效。", muted=True).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 18))
        self.label(page, "参数").grid(row=1, column=0, sticky="w", pady=(0, 12))
        self.label(page, "慢速 · 稳定优先").grid(row=1, column=1, sticky="w", padx=(14, 0), pady=(0, 12))
        self.label(page, "快速 · 整块发送").grid(row=1, column=2, sticky="w", padx=(14, 0), pady=(0, 12))
        for row, (key, label, hint, lo, hi) in enumerate(PROFILE_FIELDS, 2):
            cell = self.tk.Frame(page, bg="#FFFFFF")
            cell.grid(row=row, column=0, sticky="w", pady=7)
            self.label(cell, label).pack(anchor="w")
            self.label(cell, hint, muted=True).pack(anchor="w", pady=(2, 0))
            for column, mode in enumerate(("slow", "fast"), 1):
                self.spin(page, prefix + (mode, key), label, lo, hi).grid(row=row, column=column, sticky="ew", padx=(14, 0), pady=7)
        for column in (1, 2):
            page.grid_columnconfigure(column, weight=1)

    def build_remote(self, page):
        self.label(page, "只为远程桌面客户端使用独立速度；其他程序使用通用速度。",
                   muted=True, wraplength=580, justify="left").pack(anchor="w", pady=(0, 8))
        path = ("remote_desktop", "enabled")
        value = self.tk.BooleanVar(master=self.root)
        checkbox = self.ttk.Checkbutton(page, text="启用远程桌面专用速度", variable=value)
        checkbox.pack(anchor="w", pady=(0, 10))
        self.variables[path], self.inputs[path] = value, checkbox
        self.label(page, "客户端进程名（多个用逗号分隔）").pack(anchor="w")
        path = ("remote_desktop", "executables")
        value = self.tk.StringVar(master=self.root)
        entry = self.ttk.Entry(page, textvariable=value)
        entry.pack(fill="x", pady=(6, 6))
        self.variables[path], self.inputs[path] = value, entry
        self.label(page, "默认 mstsc.exe / msrdc.exe。其他客户端请按实际进程名填写。\n浏览器中的远程桌面不会自动识别；不建议把整个浏览器加入此列表。",
                   muted=True, wraplength=580, justify="left").pack(anchor="w", pady=(0, 15))
        path = ("remote_desktop", "citrix_enabled")
        value = self.tk.BooleanVar(master=self.root)
        checkbox = self.ttk.Checkbutton(page, text="Citrix Workspace 也使用此页速度", variable=value)
        checkbox.pack(anchor="w", pady=(0, 8))
        self.variables[path], self.inputs[path] = value, checkbox
        path = ("remote_desktop", "citrix_executables")
        value = self.tk.StringVar(master=self.root)
        entry = self.ttk.Entry(page, textvariable=value)
        entry.pack(fill="x", pady=(0, 6))
        self.variables[path], self.inputs[path] = value, entry
        self.label(page, "Citrix 会话进程（逗号分隔），支持 ICA 引擎及 Desktop Viewer。\n仅本地 Windows 客户端；浏览器 HTML5 会话不会自动识别。",
                   muted=True, wraplength=580, justify="left").pack(anchor="w", pady=(0, 15))
        grid = self.tk.Frame(page, bg="#FFFFFF")
        grid.pack(fill="x")
        self.build_speed(grid, ("remote_desktop", "profiles"))
        self.label(page, "运行本程序的电脑将按客户端进程匹配。切换焦点仍会暂停，\n这些参数不提供后台输入，也不能确认远端是否接收了每个字符。",
                   muted=True, wraplength=580, justify="left").pack(anchor="w", pady=(16, 0))

    def build_hotkeys(self, page):
        self.label(page, "输入组合，或点击「录制」后按组合键；Esc 取消录制。", muted=True).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 18))
        for row, (key, label) in enumerate(HOTKEY_FIELDS, 1):
            self.label(page, label).grid(row=row, column=0, sticky="w", padx=(0, 26), pady=10)
            path = ("hotkeys", key)
            variable = self.tk.StringVar(master=self.root)
            widget = self.ttk.Entry(page, textvariable=variable, width=28)
            widget.grid(row=row, column=1, sticky="ew", pady=10)
            self.variables[path], self.inputs[path] = variable, widget
            button = self.ttk.Button(page, text="录制", command=lambda action=key: self.begin_record(action))
            button.grid(row=row, column=2, padx=(12, 0), pady=10)
            self.record_buttons[key] = button
        page.grid_columnconfigure(1, weight=1)
        self.label(page, "字母 / 数字必须配 Ctrl、Alt 或 Win；仅 Shift 不接受。\nEsc 只在原目标窗口中止任务。F12 为系统保留键。\n录制不会执行已绑定的动作；保存时检查重复和占用。", muted=True, justify="left").grid(row=7, column=0, columnspan=3, sticky="w", pady=(22, 6))

    def begin_record(self, action):
        if self.saving:
            return
        if self.recording:
            self.cancel_record()
            return
        hwnd = self.window_handle()
        if not hwnd:
            raise RuntimeError("无法识别设置窗口，请重新打开设置后录制")
        self.record_token += 1
        self.recording = action
        self.set_editable(False)
        self.record_buttons[action].state(["!disabled"])
        self.record_buttons[action].configure(text="取消")
        self.status.configure(text="正在准备录制…", fg=self.BLUE)
        self.notify("record_start", data={"action": action, "token": self.record_token,
                                          "hwnd": hwnd})
        self.record_timer = self.root.after(15000, self.cancel_record)

    def end_record_ui(self):
        if self.record_timer is not None:
            self.root.after_cancel(self.record_timer)
            self.record_timer = None
        if self.recording:
            self.record_buttons[self.recording].configure(text="录制")
        self.recording = None
        self.set_editable(True)

    def cancel_record(self):
        if self.recording:
            self.notify("record_cancel", data=self.record_token)
            self.end_record_ui()
            # Invalidate a result already queued by the hook before cancellation.
            self.record_token += 1
            self.status.configure(text="录制已取消；原快捷键未变。", fg=self.MUTED)

    def record_focus_check(self):
        if self.recording and self.root.focus_displayof() is None:
            self.cancel_record()

    def record_result(self, result):
        if not self.recording or result["token"] != self.record_token:
            return
        if result.get("label"):
            self.variables[("hotkeys", result["action"])].set(result["label"])
        if result["done"]:
            self.end_record_ui()
        self.status.configure(text=result["text"], fg=self.BLUE)

    def build_options(self, page):
        for key, label, description in BOOL_FIELDS:
            path = ("options", key)
            variable = self.tk.BooleanVar(master=self.root)
            checkbox = self.ttk.Checkbutton(page, text=label, variable=variable)
            checkbox.pack(anchor="w", pady=(5, 0))
            self.label(page, description, muted=True, wraplength=590, justify="left").pack(anchor="w", padx=(23, 0), pady=(0, 8))
            self.variables[path], self.inputs[path] = variable, checkbox
        numbers = self.tk.Frame(page, bg="#FFFFFF")
        numbers.pack(fill="x", pady=(10, 0))
        for row, (key, label, lo, hi) in enumerate(NUMERIC_FIELDS):
            self.label(numbers, label + "（ms）").grid(row=row, column=0, sticky="w", padx=(0, 20), pady=6)
            self.spin(numbers, ("options", key), label, lo, hi, 14).grid(row=row, column=1, sticky="w", pady=6)

    def populate(self, settings):
        for path, variable in self.variables.items():
            value = settings
            for part in path:
                value = value[part]
            if path in (("remote_desktop", "executables"), ("remote_desktop", "citrix_executables")):
                value = ", ".join(value)
            variable.set(value)

    def open(self, settings, path):
        if not self.visible:
            self.settings, self.path = copy.deepcopy(settings), path
            self.populate(settings)
            self.location.configure(text="配置文件：" + path)
            self.status.configure(text="保存后立即应用快捷键；速度与输入保护从下一次任务生效。", fg=self.MUTED)
        self.visible = True
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def reset(self):
        if not self.saving and not self.recording:
            self.populate(self.defaults)
            self.status.configure(text="已填入默认值；点击「保存并应用」后生效。", fg=self.MUTED)

    def collect(self):
        candidate = copy.deepcopy(self.settings)
        for path, variable in self.variables.items():
            value = variable.get()
            if path in self.integer_labels:
                try:
                    value = int(value.strip())
                except (ValueError, AttributeError):
                    self.inputs[path].focus_set()
                    raise ValueError(self.integer_labels[path] + "必须填写整数") from None
            elif path[0] == "hotkeys":
                value = value.strip()
            elif path in (("remote_desktop", "executables"), ("remote_desktop", "citrix_executables")):
                value = [name.strip() for name in value.replace("，", ",").split(",") if name.strip()]
            target = candidate
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
        return self.validator(candidate)

    def save(self):
        if self.saving or self.recording:
            return
        try:
            candidate = self.collect()
        except Exception as exc:
            self.status.configure(text="未保存：" + str(exc), fg="#C14450")
            return
        self.saving = True
        self.set_editable(False)
        self.status.configure(text="正在保存并检查快捷键…", fg=self.MUTED)
        self.notify("gui_save", data=candidate)

    def set_editable(self, enabled):
        state = "!disabled" if enabled else "disabled"
        self.save_button.state([state])
        self.reset_button.state([state])
        for widget in self.inputs.values():
            widget.state([state])
        for button in self.record_buttons.values():
            button.state([state])

    def save_result(self, result):
        self.saving = False
        self.set_editable(True)
        if result["ok"]:
            self.settings = copy.deepcopy(result["settings"])
            self.populate(self.settings)
            self.status.configure(text="已保存并应用。回到原输入位置后，按暂停 / 继续快捷键恢复任务。", fg="#168569")
        else:
            self.status.configure(text="未保存：" + result["error"], fg="#C14450")

    def close(self):
        if self.saving:
            self.status.configure(text="正在保存，请稍候再关闭。", fg=self.MUTED)
            return
        self.cancel_record()
        self.visible = False
        self.root.withdraw()
        self.notify("gui_closed")

    def callback_error(self, kind, error, tb):
        self.cancel_record()
        self.status.configure(text="窗口操作失败：" + str(error), fg="#C14450")
        self.notify("gui_error", str(error), "".join(traceback.format_exception(kind, error, tb)))
