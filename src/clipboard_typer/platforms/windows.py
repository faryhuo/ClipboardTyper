"""Windows ABI types, native API bindings and process coordination."""
import ctypes as C
import ntpath
import queue
import threading


UINT = DWORD = C.c_uint32
WORD = C.c_uint16
LONG = BOOL = C.c_int32
HANDLE = HWND = C.c_void_p
WPARAM = ULONG_PTR = C.c_size_t
LPARAM = LRESULT = C.c_ssize_t


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD),
                ("dwFlags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class INPUTUNION(C.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(C.Structure):
    # 必须包含完整 union，否则 64 位 SendInput 的 cbSize 会错误。
    _anonymous_ = ("data",)
    _fields_ = [("type", DWORD), ("data", INPUTUNION)]


class POINT(C.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(C.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class MSG(C.Structure):
    _fields_ = [("hwnd", HWND), ("message", UINT), ("wParam", WPARAM),
                ("lParam", LPARAM), ("time", DWORD), ("pt", POINT),
                ("lPrivate", DWORD)]


class KBDLLHOOKSTRUCT(C.Structure):
    _fields_ = [("vkCode", DWORD), ("scanCode", DWORD), ("flags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class WNDCLASSW(C.Structure):
    _fields_ = [("style", UINT), ("lpfnWndProc", C.c_void_p),
                ("cbClsExtra", C.c_int32), ("cbWndExtra", C.c_int32),
                ("hInstance", HANDLE), ("hIcon", HANDLE), ("hCursor", HANDLE),
                ("hbrBackground", HANDLE), ("lpszMenuName", C.c_wchar_p),
                ("lpszClassName", C.c_wchar_p)]


class GUID(C.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", WORD), ("Data3", WORD),
                ("Data4", C.c_ubyte * 8)]


class NOTIFYICONDATAW(C.Structure):
    # WCHAR 固定为 16 位，便于在不同开发环境核对 Windows ABI 布局。
    _fields_ = [("cbSize", DWORD), ("hWnd", HWND), ("uID", UINT),
                ("uFlags", UINT), ("uCallbackMessage", UINT), ("hIcon", HANDLE),
                ("szTip", WORD * 128), ("dwState", DWORD), ("dwStateMask", DWORD),
                ("szInfo", WORD * 256), ("uVersion", UINT),
                ("szInfoTitle", WORD * 64), ("dwInfoFlags", DWORD),
                ("guidItem", GUID), ("hBalloonIcon", HANDLE)]


VK_TAB, VK_RETURN, VK_SHIFT, VK_CONTROL, VK_MENU = 9, 13, 16, 17, 18
VK_ESCAPE, VK_HOME, VK_DELETE, VK_LWIN, VK_RWIN = 27, 36, 46, 91, 92
VK_F8 = 0x77
KEYUP, UNICODE, EXTENDED = 0x0002, 0x0004, 0x0001
CF_UNICODETEXT, CF_HDROP = 13, 15
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 1, 2, 0x4000
INFINITE, WAIT_FAILED = 0xFFFFFFFF, 0xFFFFFFFF
MODIFIER_KEYS = frozenset((VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN,
                           0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5))
SIDE_MODIFIER_KEYS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, VK_LWIN, VK_RWIN)

class EventBus:
    def __init__(self, win):
        self.win, self.queue, self.lock = win, queue.SimpleQueue(), threading.Lock()
        self.handle = win.CreateEventW(None, False, False, None)  # auto-reset
        if not self.handle:
            raise C.WinError(C.get_last_error())

    def put(self, event):
        with self.lock:
            if self.handle:
                self.queue.put(event)
                self.win.SetEvent(self.handle)

    def drain(self):
        while True:
            try:
                yield self.queue.get_nowait()
            except queue.Empty:
                return

    def close(self):
        with self.lock:
            if self.handle:
                self.win.CloseHandle(self.handle)
                self.handle = None


def key_event(vk=0, scan=0, flags=0):
    event = INPUT()
    event.type = 1  # INPUT_KEYBOARD
    event.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return event


def key_pair(vk):
    flags = EXTENDED if vk in (VK_HOME, VK_DELETE) else 0
    return [key_event(vk, flags=flags), key_event(vk, flags=flags | KEYUP)]


def text_events(text):
    """每个 UTF-16 单元各有 down/up；不会把中文当作键盘快捷键。"""
    events = []
    for char in text:
        if char == "\t":
            events.extend(key_pair(VK_TAB))
        elif char == "\b":
            events.extend(key_pair(8))
        else:
            data = char.encode("utf-16-le", errors="surrogatepass")
            for offset in range(0, len(data), 2):
                unit = int.from_bytes(data[offset:offset + 2], "little")
                events.extend((key_event(scan=unit, flags=UNICODE),
                               key_event(scan=unit, flags=UNICODE | KEYUP)))
    return events

class Win32:
    def __init__(self):
        self.user = C.WinDLL("user32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.gdi = C.WinDLL("gdi32", use_last_error=True)
        self.shell = C.WinDLL("shell32", use_last_error=True)
        self.HOOKPROC = C.WINFUNCTYPE(LRESULT, C.c_int, WPARAM, LPARAM)
        self.WNDPROC = C.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

        def bind(dll, name, result, *args):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = result, list(args)
            setattr(self, name, fn)

        bind(self.user, "SendInput", UINT, UINT, C.POINTER(INPUT), C.c_int)
        bind(self.user, "GetForegroundWindow", HWND)
        bind(self.user, "GetWindowThreadProcessId", DWORD, HWND, C.POINTER(DWORD))
        bind(self.kernel, "OpenProcess", HANDLE, DWORD, BOOL, DWORD)
        bind(self.kernel, "QueryFullProcessImageNameW", BOOL, HANDLE, DWORD, C.c_wchar_p, C.POINTER(DWORD))
        bind(self.user, "IsWindow", BOOL, HWND)
        bind(self.user, "IsIconic", BOOL, HWND)
        bind(self.user, "GetAsyncKeyState", C.c_int16, C.c_int)
        bind(self.user, "RegisterHotKey", BOOL, HWND, C.c_int, UINT, UINT)
        bind(self.user, "UnregisterHotKey", BOOL, HWND, C.c_int)
        bind(self.user, "PeekMessageW", BOOL, C.POINTER(MSG), HWND, UINT, UINT, UINT)
        bind(self.user, "TranslateMessage", BOOL, C.POINTER(MSG))
        bind(self.user, "DispatchMessageW", LRESULT, C.POINTER(MSG))
        bind(self.user, "MsgWaitForMultipleObjectsEx", DWORD, DWORD, C.POINTER(HANDLE), DWORD, DWORD, DWORD)
        bind(self.user, "SetWindowsHookExW", HANDLE, C.c_int, self.HOOKPROC, HANDLE, DWORD)
        bind(self.user, "CallNextHookEx", LRESULT, HANDLE, C.c_int, WPARAM, LPARAM)
        bind(self.user, "UnhookWindowsHookEx", BOOL, HANDLE)
        bind(self.user, "OpenClipboard", BOOL, HWND)
        bind(self.user, "CloseClipboard", BOOL)
        bind(self.user, "IsClipboardFormatAvailable", BOOL, UINT)
        bind(self.user, "GetClipboardData", HANDLE, UINT)
        bind(self.user, "MessageBoxW", C.c_int, HWND, C.c_wchar_p, C.c_wchar_p, UINT)
        bind(self.user, "CreateWindowExW", HWND, DWORD, C.c_wchar_p, C.c_wchar_p,
             DWORD, C.c_int, C.c_int, C.c_int, C.c_int, HWND, HANDLE, HANDLE, C.c_void_p)
        bind(self.user, "SetWindowTextW", BOOL, HWND, C.c_wchar_p)
        bind(self.user, "SetWindowPos", BOOL, HWND, HWND, C.c_int, C.c_int,
             C.c_int, C.c_int, UINT)
        bind(self.user, "ShowWindow", BOOL, HWND, C.c_int)
        bind(self.user, "DestroyWindow", BOOL, HWND)
        bind(self.user, "SendMessageW", LRESULT, HWND, UINT, WPARAM, LPARAM)
        bind(self.user, "SystemParametersInfoW", BOOL, UINT, UINT, C.c_void_p, UINT)
        bind(self.user, "RegisterClassW", WORD, C.POINTER(WNDCLASSW))
        bind(self.user, "UnregisterClassW", BOOL, C.c_wchar_p, HANDLE)
        bind(self.user, "DefWindowProcW", LRESULT, HWND, UINT, WPARAM, LPARAM)
        bind(self.user, "RegisterWindowMessageW", UINT, C.c_wchar_p)
        bind(self.user, "LoadIconW", HANDLE, HANDLE, C.c_void_p)
        bind(self.user, "SetForegroundWindow", BOOL, HWND)
        bind(self.user, "PostMessageW", BOOL, HWND, UINT, WPARAM, LPARAM)
        bind(self.user, "GetCursorPos", BOOL, C.POINTER(POINT))
        bind(self.user, "CreatePopupMenu", HANDLE)
        bind(self.user, "AppendMenuW", BOOL, HANDLE, UINT, ULONG_PTR, C.c_wchar_p)
        bind(self.user, "TrackPopupMenu", UINT, HANDLE, UINT, C.c_int, C.c_int,
             C.c_int, HWND, C.POINTER(RECT))
        bind(self.user, "DestroyMenu", BOOL, HANDLE)
        bind(self.user, "EndMenu", BOOL)
        bind(self.shell, "Shell_NotifyIconW", BOOL, DWORD, C.POINTER(NOTIFYICONDATAW))
        bind(self.shell, "DragQueryFileW", UINT, HANDLE, UINT, C.c_wchar_p, UINT)
        bind(self.gdi, "GetStockObject", HANDLE, C.c_int)
        bind(self.kernel, "GetModuleHandleW", HANDLE, C.c_wchar_p)
        bind(self.kernel, "GlobalLock", C.c_void_p, HANDLE)
        bind(self.kernel, "GlobalUnlock", BOOL, HANDLE)
        bind(self.kernel, "GlobalSize", C.c_size_t, HANDLE)
        bind(self.kernel, "CreateMutexW", HANDLE, C.c_void_p, BOOL, C.c_wchar_p)
        bind(self.kernel, "ReleaseMutex", BOOL, HANDLE)
        bind(self.kernel, "CreateEventW", HANDLE, C.c_void_p, BOOL, BOOL, C.c_wchar_p)
        bind(self.kernel, "OpenEventW", HANDLE, DWORD, BOOL, C.c_wchar_p)
        bind(self.kernel, "SetEvent", BOOL, HANDLE)
        bind(self.kernel, "ResetEvent", BOOL, HANDLE)
        bind(self.kernel, "WaitForSingleObject", DWORD, HANDLE, DWORD)
        bind(self.kernel, "CloseHandle", BOOL, HANDLE)

    def application_executable(self, hwnd):
        if not hwnd:
            return ""
        pid = DWORD()
        self.GetWindowThreadProcessId(hwnd, C.byref(pid))
        process = self.OpenProcess(0x1000, False, pid.value) if pid.value else None
        if not process:
            return ""
        try:
            buffer = C.create_unicode_buffer(32768)
            size = DWORD(len(buffer))
            if self.QueryFullProcessImageNameW(process, 0, buffer, C.byref(size)):
                return ntpath.basename(buffer.value).casefold()
            return ""
        finally:
            self.CloseHandle(process)

    def key_down(self, vk):
        return bool(self.GetAsyncKeyState(vk) & 0x8000)

    def send(self, events):
        if not events:
            return
        array = (INPUT * len(events))(*events)
        C.set_last_error(0)
        sent = self.SendInput(len(array), array, C.sizeof(INPUT))
        if sent != len(array):
            error = C.get_last_error()
            # 失败时尽力补发已按下按键的 key-up，特别是 Shift。
            releases = [key_event(e.ki.wVk, e.ki.wScan, e.ki.dwFlags | KEYUP)
                        for e in reversed(events[:sent]) if not e.ki.dwFlags & KEYUP]
            if releases:
                cleanup = (INPUT * len(releases))(*releases)
                self.SendInput(len(cleanup), cleanup, C.sizeof(INPUT))
            raise RuntimeError(
                f"SendInput 失败：{sent}/{len(array)} 个事件，错误码 {error}。"
                "请检查目标程序权限及是否接受模拟键盘输入。")


class SingleInstance:
    """用命名事件请求旧实例有序退出，不强制终止任意进程。"""
    MUTEX = "Local\\PythonClipboardTyper_93ad601e_Mutex"
    EVENT = "Local\\PythonClipboardTyper_93ad601e_Replace"

    def __init__(self, win):
        self.win, self.mutex, self.event, self.owned = win, None, None, False

    def acquire(self):
        w = self.win
        self.mutex = w.CreateMutexW(None, False, self.MUTEX)
        if not self.mutex:
            raise C.WinError(C.get_last_error())
        status = w.WaitForSingleObject(self.mutex, 0)
        if status == 0x102:  # WAIT_TIMEOUT：旧实例持有互斥锁。
            old_event = w.OpenEventW(0x0002, False, self.EVENT)
            if old_event:
                try:
                    w.SetEvent(old_event)
                finally:
                    w.CloseHandle(old_event)
            status = w.WaitForSingleObject(self.mutex, 3000)
        if status not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
            raise RuntimeError("旧实例尚未退出，请先按 Ctrl+Alt+Q，再重新运行。")
        self.owned = True
        self.event = w.CreateEventW(None, True, False, self.EVENT)
        if not self.event or not w.ResetEvent(self.event):
            raise C.WinError(C.get_last_error())

    def replacement_requested(self):
        return self.win.WaitForSingleObject(self.event, 0) == 0

    def close(self):
        if self.event:
            self.win.CloseHandle(self.event)
        if self.owned:
            self.win.ReleaseMutex(self.mutex)
        if self.mutex:
            self.win.CloseHandle(self.mutex)
