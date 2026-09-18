"""Native non-activating status card and its drawing helpers."""
from contextlib import contextmanager
import ctypes as C
import time
import traceback
from clipboard_typer.ui.branding import BLUE, INK, LOGO_RECTS


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


def card_scene(message, snapshot=None, error=False, pause_key="F8", notice=False):
    """Shared logical layout for Windows painting and deterministic layout checks."""
    snapshot = snapshot or {}
    state = snapshot.get("state", "就绪")
    accent, tint = BLUE, "#EAF1FF"
    if error:
        title, badge, accent, tint = "需要处理", "错误", "#D14C57", "#FFF0F1"
    elif notice:
        title, badge = "操作提示", snapshot.get("label", "提示")
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
    progress = bool(total) and not error and not notice
    detail = message.split("\n", 1)[0].replace("错误：", "", 1)
    if progress:
        detail = f"已发送 {snapshot['sent']:,} / {total:,} 个字符"
    ops = [
        ("round", (0, 0, 380, 190), "#E0E6F0", 18),
        ("round", (1, 1, 379, 189), "#FFFFFF", 17),
        *[("round", (18+a*.5, 15+b*.5, 18+c*.5, 15+d*.5), color, radius*.5)
          for a, b, c, d, radius, color in LOGO_RECTS],
        ("text", (58, 17, 244, 44), "ClipboardTyper · Citrix" if snapshot.get("profile_name") == "Citrix Workspace" else "ClipboardTyper · RDP" if snapshot.get("profile_name") == "远程桌面" else "ClipboardTyper", "#546178", 12, True, "left"),
        ("text", (302, 13, 330, 44), "···", "#68768C", 18, True, "center"),
        ("text", (342, 14, 367, 43), "×", "#8793A5", 18, False, "center"),
        ("text", (20, 56, 210, 88), title, INK, 23, True, "left"),
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
        self.notice = False
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

    def set_context(self, snapshot=None, error=False, pause_key="F8", notice=False):
        self.snapshot, self.error, self.pause_key = snapshot, error, pause_key
        self.notice = notice

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
            for op in card_scene(self.current or "", self.snapshot, self.error, self.pause_key, self.notice):
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
