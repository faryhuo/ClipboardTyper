#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 剪贴板模拟输入器：Python 3.9+，仅使用标准库，无需 AutoHotkey。

运行：
    python clipboard_typer.py
    或 py -3 clipboard_typer.py
    双击 build_exe.bat 自动安装 PyInstaller，在 dist 文件夹生成 ClipboardTyper.exe。

运行后显示右下角状态卡片及系统托盘。右键可隐藏卡片、打开「设置…」或退出。
配置 GUI 使用 Python 标准库 tkinter；请将 clipboard_typer_ui.py 与本文件放在同一目录。
如果图标被 Windows 收起，请点击时钟旁的 ^ 展开隐藏图标。
打包后的 exe 无控制台窗口，不需要安装 Python 或 AutoHotkey。

用法：先复制文本，点击目标输入位置，再按快捷键并松开。以下为默认快捷键：
    Ctrl+J      慢速输入
    Ctrl+K      快速输入
    F8          暂停 / 继续当前输入（保留剩余文本）
    Esc         原目标窗口在前台时中止输入；其他软件中的 Esc 不影响暂停任务
    Ctrl+Alt+S  中止当前输入
    Ctrl+Alt+Q  退出程序

速度和快捷键配置在 EXE / 脚本旁的 settings.json，时间单位为毫秒。
双击托盘或选择「设置…」可直接调整参数并保存，无需编辑 JSON 或重新打包。
速度和输入选项从下一次任务生效；仍支持编辑、重新加载 JSON 配置。
设置中的「远程桌面」为匹配的远程客户端提供独立速度，其他应用使用通用速度。
卡片跟随目标屏幕并支持每屏缩放，可拖动；右键可恢复跟随。
options.clear_auto_indent=true 保留原脚本的 Enter、Shift+Home、Delete 行为。
如果目标编辑器不需要清除自动缩进，可改为 false。
这组按键会修改当前行；自动补全、自动配对、Tab 的行为仍由目标软件决定。

说明：
* 模拟键盘输入，不使用 Ctrl+V，不修改剪贴板；keyDelay<=0 按 chunk 整块发送。
* 使用 Unicode 输入，支持中文及 UTF-16 代理对字符（例如 emoji）。
* 焦点保护针对顶层窗口，与原脚本一样，不区分同一窗口内的不同输入框。
* 默认切换窗口会自动暂停；切回原窗口后按 F8 继续。
* 托盘菜单的「继续输入」会尝试激活原窗口；激活失败时保持暂停。
* 暂停只保存在本次运行中；中止、退出或重新启动会丢弃剩余任务。
* 暂停不会重新读取剪贴板；请勿移动原输入框光标或改动已输入的文本。
* 打开托盘菜单会自动暂停，关闭菜单后保持暂停；菜单中的 Esc 只关闭菜单。
* 已送入 Windows 输入队列的事件无法撤回，当前按键组可能先完成。
* 物理 Ctrl / Alt / Shift / Win 自动暂停；松开后按暂停/继续快捷键恢复。
* 状态窗显示已发送字符数、百分比、行号和剩余量；错误保留到确认，并写入日志。
* 空闲主线程阻塞等待 Windows 消息和事件，不定时轮询键盘。
* 目标以管理员权限运行时，本程序也需同等权限。
* 重复启动会通知旧实例退出，然后接管快捷键，对应 #SingleInstance Force。
* Python 和 AutoHotkey 的调度不同，保留参数值，但速度不是逐毫秒完全等同。

Windows API 参考：
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput
https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-keybdinput
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey
https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc
"""

import ctypes as C
import copy
import json
import logging
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
import math
import ntpath
import os
import queue
import subprocess
import sys
import threading
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from clipboard_typer_ui import StatusCard, ConfigService


# ── 默认配置；实际修改 settings.json，掉字时增大延迟或减小 chunk ──
DEFAULT_PROFILES = {
    "slow": dict(keyDelay=10, chunk=1, pause=50,
                 breatherEvery=10, breatherPause=400, linePause=400),
    "fast": dict(keyDelay=0, chunk=12, pause=15,
                 breatherEvery=8, breatherPause=80, linePause=120),
}
DEFAULT_SETTINGS = {
    "version": 1,
    "profiles": DEFAULT_PROFILES,
    "remote_desktop": {
        "enabled": True,
        "executables": ["mstsc.exe", "msrdc.exe"],
        "citrix_enabled": True,
        "citrix_executables": ["wfica32.exe", "cdviewer.exe", "citrix.desktopviewer.app.exe"],
        "profiles": {
            "slow": dict(DEFAULT_PROFILES["slow"]),
            "fast": dict(keyDelay=0, chunk=4, pause=35,
                         breatherEvery=8, breatherPause=160, linePause=250),
        },
    },
    "hotkeys": {"slow": "Ctrl+J", "fast": "Ctrl+K", "pause_resume": "F8",
                "stop": "Ctrl+Alt+S", "quit": "Ctrl+Alt+Q"},
    "options": {"stop_on_focus_loss": True, "pause_on_focus_loss": True,
                "clear_auto_indent": True, "pause_on_modifiers": True,
                "start_delay_ms": 200, "show_popup": True, "show_progress": True,
                "progress_interval_ms": 250, "notice_duration_ms": 4000},
}

# ── Windows 类型：指针大小随 Python 位数变化；DWORD/LONG 固定 32 位 ──
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
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 1, 2, 0x4000
INFINITE, WAIT_FAILED = 0xFFFFFFFF, 0xFFFFFFFF
MODIFIER_KEYS = frozenset((VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN,
                           0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5))
SIDE_MODIFIER_KEYS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, VK_LWIN, VK_RWIN)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Hotkey:
    action: str
    modifiers: int
    key: int
    label: str

    @property
    def combo(self):
        return self.modifiers, self.key


@dataclass
class UiEvent:
    kind: str
    text: str = ""
    detail: str = ""
    data: object = None


def parse_hotkey(action, value):
    if not isinstance(value, str):
        raise ConfigError(f"hotkeys.{action} 必须是字符串")
    parts = [part.strip().upper() for part in value.split("+")]
    if not parts or any(not part for part in parts):
        raise ConfigError(f"快捷键格式无效：{value!r}")
    names = {"CTRL": 2, "CONTROL": 2, "ALT": 1, "SHIFT": 4, "WIN": 8}
    modifiers = 0
    for part in parts[:-1]:
        if part not in names or modifiers & names[part]:
            raise ConfigError(f"快捷键修饰键无效或重复：{value}")
        modifiers |= names[part]
    key = parts[-1]
    special = {"PAUSE": 0x13, "SCROLLLOCK": 0x91, "INSERT": 0x2D,
               "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22}
    if len(key) == 1 and key in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        if not modifiers & (1 | 2 | 8):
            raise ConfigError("字母和数字快捷键必须搭配 Ctrl、Alt 或 Win；仅 Shift 仍会影响正常打字")
        vk = ord(key)
    elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
        if vk == 0x7B:
            raise ConfigError("F12 由 Windows 调试器保留，请选择其他快捷键")
    elif key in special:
        vk = special[key]
    else:
        raise ConfigError(f"不支持的快捷键：{value}；可用字母、数字、F1–F24（除 F12）等。Esc 为固定中止键。")
    return Hotkey(action, modifiers, vk, value.strip())


def validate_settings(raw):
    if not isinstance(raw, dict):
        raise ConfigError("settings.json 顶层必须是 JSON 对象")
    result = copy.deepcopy(DEFAULT_SETTINGS)

    def merge(target, source, label):
        if not isinstance(source, dict):
            raise ConfigError(label + " 必须是 JSON 对象")
        for key, value in source.items():
            if key not in target:
                raise ConfigError(f"未知配置项：{label}.{key}")
            if isinstance(target[key], dict):
                merge(target[key], value, label + "." + key)
            else:
                target[key] = value

    merge(result, raw, "settings")
    if type(result["version"]) is not int or result["version"] != 1:
        raise ConfigError("不支持此配置版本，version 必须为 1")
    ranges = {"keyDelay": (-1, 60000), "chunk": (1, 256), "pause": (0, 60000),
              "breatherEvery": (0, 1000000), "breatherPause": (0, 60000), "linePause": (0, 60000)}
    for prefix, profiles in (("profiles", result["profiles"]),
                             ("remote_desktop.profiles", result["remote_desktop"]["profiles"])):
        for mode, profile in profiles.items():
            for key, (minimum, maximum) in ranges.items():
                value = profile[key]
                if type(value) is not int or not minimum <= value <= maximum:
                    raise ConfigError(f"{prefix}.{mode}.{key} 必须是 {minimum}–{maximum} 的整数")
    remote = result["remote_desktop"]
    for field in ("enabled", "citrix_enabled"):
        if type(remote[field]) is not bool:
            raise ConfigError(f"remote_desktop.{field} 必须为 true 或 false")
    for field in ("executables", "citrix_executables"):
        names = remote[field]
        if not isinstance(names, list) or not 1 <= len(names) <= 16:
            raise ConfigError("远程客户端进程名必须是 1–16 项的列表")
        normalized = []
        for name in names:
            if (not isinstance(name, str) or not name.strip() or len(name) > 128
                    or any(char in name for char in '\\/:*?"<>|') or not name.strip().lower().endswith(".exe")):
                raise ConfigError("客户端请填写进程名，例如 mstsc.exe，不填写路径或通配符")
            normalized.append(name.strip().lower())
        remote[field] = list(dict.fromkeys(normalized))
    numeric_options = {"start_delay_ms": (0, 60000), "progress_interval_ms": (100, 5000),
                       "notice_duration_ms": (1000, 60000)}
    for key, value in result["options"].items():
        if key in numeric_options:
            lo, hi = numeric_options[key]
            if type(value) is not int or not lo <= value <= hi:
                raise ConfigError(f"options.{key} 必须是 {lo}–{hi} 的整数")
        elif type(value) is not bool:
            raise ConfigError(f"options.{key} 必须为 true 或 false")
    hotkeys = [parse_hotkey(action, value) for action, value in result["hotkeys"].items()]
    if len({key.combo for key in hotkeys}) != len(hotkeys):
        raise ConfigError("多个动作使用了相同快捷键，请分别设置")
    return result


def select_speed_profile(settings, executable, mode):
    """Only Remote Desktop has an override; all other applications use global speeds."""
    name = ntpath.basename(executable or "").casefold()
    remote = settings["remote_desktop"]
    citrix = (remote["enabled"] and remote["citrix_enabled"]
              and name in {item.casefold() for item in remote["citrix_executables"]})
    matched = citrix or (remote["enabled"] and name in {item.casefold() for item in remote["executables"]})
    profiles = remote["profiles"] if matched else settings["profiles"]
    return copy.deepcopy(profiles[mode]), "Citrix Workspace" if citrix else "远程桌面" if matched else "通用"


def read_settings(path):
    if path.stat().st_size > 65536:
        raise ConfigError("settings.json 超过 64 KiB，请检查文件内容")
    return validate_settings(json.loads(path.read_text(encoding="utf-8-sig")))


def save_settings_atomic(path, settings):
    """Replace a complete JSON file atomically; failed writes keep the old file."""
    path = Path(path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".clipboard-settings-",
                                         suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def application_paths():
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    data_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ClipboardTyper"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_path = base / "settings.json"
    if not settings_path.exists():
        content = json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2) + "\n"
        try:
            with settings_path.open("x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            pass
        except OSError:
            settings_path = data_dir / "settings.json"
            if not settings_path.exists():
                settings_path.write_text(content, encoding="utf-8")
    return settings_path, data_dir / "clipboard_typer.log"


class DeferredLogHandler(QueueHandler):
    def prepare(self, record):
        # Keep traceback formatting and file rotation on the consumer thread too.
        return copy.copy(record)


def close_logger(logger):
    listener = getattr(logger, "background_listener", None)
    if listener:
        listener.stop()  # Drain after the keyboard hook has been removed.
        for handler in listener.handlers:
            handler.close()
        logger.background_listener = None
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def make_logger(path):
    logger = logging.getLogger("ClipboardTyper")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    close_logger(logger)
    handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    messages = queue.SimpleQueue()
    logger.addHandler(DeferredLogHandler(messages))
    logger.background_listener = QueueListener(messages, handler)
    logger.background_listener.start()
    return logger


class HotkeyManager:
    """先注册新增组合，全部成功后才释放旧组合；失败保留原绑定。"""
    def __init__(self, win, hwnd):
        self.win, self.hwnd = win, hwnd
        self.bindings, self.actions, self.next_id = {}, {}, 1
        self.pending = None

    def stage(self, values):
        if self.pending is not None:
            raise ConfigError("正在保存快捷键，请稍候")
        desired = [parse_hotkey(action, value) for action, value in values.items()]
        if len({key.combo for key in desired}) != len(desired):
            raise ConfigError("快捷键重复")
        staged = {}
        try:
            for key in desired:
                if key.combo in self.bindings:
                    continue
                hotkey_id = self.next_id
                self.next_id += 1
                if hotkey_id > 0xBFFF:
                    raise ConfigError("快捷键重载次数过多，请重新启动程序")
                if not self.win.RegisterHotKey(self.hwnd, hotkey_id, key.modifiers | MOD_NOREPEAT, key.key):
                    raise ConfigError(f"无法注册 {key.label}，可能被其他程序占用；原快捷键保持有效")
                staged[key.combo] = hotkey_id
        except Exception:
            for hotkey_id in staged.values():
                self.win.UnregisterHotKey(self.hwnd, hotkey_id)
            raise
        self.pending = desired, staged

    def commit(self):
        desired, staged = self.pending
        combined = dict(self.bindings)
        combined.update(staged)
        bindings = {key.combo: combined[key.combo] for key in desired}
        actions = {bindings[key.combo]: key.action for key in desired}
        retired = [hotkey_id for combo, hotkey_id in self.bindings.items() if combo not in bindings]
        self.bindings, self.actions = bindings, actions
        self.pending = None
        for hotkey_id in retired:
            self.win.UnregisterHotKey(self.hwnd, hotkey_id)

    def rollback(self):
        if self.pending is not None:
            for hotkey_id in self.pending[1].values():
                self.win.UnregisterHotKey(self.hwnd, hotkey_id)
            self.pending = None

    def apply(self, values):
        self.stage(values)
        self.commit()

    def close(self):
        self.rollback()
        for hotkey_id in self.bindings.values():
            self.win.UnregisterHotKey(self.hwnd, hotkey_id)
        self.bindings.clear()
        self.actions.clear()


class PhysicalKeys:
    """只记物理事件；忽略本程序和其他程序注入的按键。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.pressed = set()

    def seed(self, win, extra_keys=()):
        with self.lock:
            for vk in set(SIDE_MODIFIER_KEYS) | set(extra_keys):
                if win.key_down(vk):
                    self.pressed.add(vk)

    def observe(self, vk, down, injected=False):
        if injected:
            return False
        with self.lock:
            if down:
                self.pressed.add(vk)
            else:
                self.pressed.discard(vk)
                generic = {0xA0: VK_SHIFT, 0xA1: VK_SHIFT, 0xA2: VK_CONTROL,
                           0xA3: VK_CONTROL, 0xA4: VK_MENU, 0xA5: VK_MENU}.get(vk)
                if generic:
                    self.pressed.discard(generic)
        return True

    def down(self, vk):
        with self.lock:
            return vk in self.pressed

    def modifiers_down(self):
        with self.lock:
            return bool(self.pressed & MODIFIER_KEYS)


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


class Cancelled(Exception):
    pass


class TypingJob:
    def __init__(self, win, cfg, label, trigger, target, shutdown, notices,
                 options=None, physical=None, shortcut=None):
        self.win, self.cfg = win, copy.deepcopy(cfg)
        self.options = copy.deepcopy(options or DEFAULT_SETTINGS["options"])
        self.label, self.trigger, self.target = label, trigger, target
        self.origin_hwnd = target
        self.shutdown, self.notices = shutdown, notices
        self.physical = physical or PhysicalKeys()
        self.shortcut = shortcut or (lambda action: DEFAULT_SETTINGS["hotkeys"][action])
        self.resume_key = parse_hotkey("pause_resume", self.shortcut("pause_resume")).key
        self.abort, self.finished = threading.Event(), threading.Event()
        self.control = threading.Condition()
        self._paused, self._resume_pending, self.armed = False, False, False
        self._reason, self._state = "", "准备输入"
        self._sent, self._total, self._line, self._lines = 0, 0, 1, 1
        self._last_progress = 0
        self.profile_name = "通用"

    def publish(self, text, kind="notice", detail=""):
        self.notices.put(UiEvent(kind, text, detail, self))

    def report_progress(self, force=False):
        now = time.monotonic()
        with self.control:
            if not force and now - self._last_progress < self.options["progress_interval_ms"] / 1000:
                return
            self._last_progress = now
        self.notices.put(UiEvent("progress", data=self))

    def snapshot(self):
        with self.control:
            state = ("等待松开快捷键" if self._resume_pending else "已暂停") if self._paused else self._state
            return {"state": state, "label": self.label, "reason": self._reason, "profile_name": self.profile_name,
                    "sent": self._sent, "total": self._total, "line": self._line, "lines": self._lines,
                    "remaining": max(0, self._total - self._sent),
                    "percent": self._sent * 100 / self._total if self._total else 0}

    def is_paused(self):
        with self.control:
            return self._paused

    def is_resuming(self):
        with self.control:
            return self._resume_pending

    def notify_key_change(self):
        with self.control:
            self.control.notify_all()

    def pause(self, reason="手动暂停"):
        with self.control:
            if self.finished.is_set() or self.abort.is_set() or self.shutdown.is_set():
                return False
            changed = not self._paused or self._resume_pending
            self._paused, self._resume_pending = True, False
            if changed:
                self._reason = reason
                self.publish("已暂停：" + reason + "；回到原窗口按 " + self.shortcut("pause_resume") + " 继续")
            self.control.notify_all()
            return True

    def request_resume(self):
        with self.control:
            if (not self._paused or self.finished.is_set()
                    or self.abort.is_set() or self.shutdown.is_set()):
                return False
            if not self._resume_pending:
                self._resume_pending = True
                self.publish("准备继续：请松开快捷键及 Ctrl / Alt / Shift / Win")
            self.control.notify_all()
            return True

    def cancel(self):
        self.abort.set()
        self.notify_key_change()

    def check_cancelled(self):
        if self.abort.is_set() or self.shutdown.is_set():
            raise Cancelled("快捷键或退出请求")
        if self.target and not self.win.IsWindow(self.target):
            raise Cancelled("原目标窗口已关闭")

    def check(self):
        while True:
            self.check_cancelled()
            with self.control:
                paused, resuming = self._paused, self._resume_pending
                if paused and not resuming:
                    # 主线程不是轮询；此低频检查只用于发现暂停任务的目标窗口已关闭。
                    self.control.wait(timeout=0.25)
                    continue
            focused = not self.target or self.win.GetForegroundWindow() == self.target
            if paused and resuming:
                if not focused:
                    with self.control:
                        if self._resume_pending:
                            self._resume_pending = False
                            self.publish("仍在暂停：请切回原输入窗口，再按 " + self.shortcut("pause_resume"))
                    continue
                if self.physical.modifiers_down() or self.physical.down(self.resume_key):
                    with self.control:
                        self.control.wait(timeout=0.05)
                    continue
                with self.control:
                    if self._paused and self._resume_pending:
                        self._paused, self._resume_pending, self._reason = False, False, ""
                        self.publish(self.label + "输入继续；" + self.shortcut("pause_resume") + " 暂停")
                continue
            if not focused:
                if self.options["pause_on_focus_loss"]:
                    self.pause("目标窗口失去焦点")
                    continue
                raise Cancelled("目标窗口失去焦点")
            if self.armed and self.options["pause_on_modifiers"] and self.physical.modifiers_down():
                self.pause("检测到手动 Ctrl / Alt / Shift / Win")
                continue
            with self.control:
                if not self._paused:
                    return

    def nap(self, milliseconds):
        self.check()
        if milliseconds <= 0:
            if milliseconds == 0:
                time.sleep(0)
            self.check()
            return
        remaining = milliseconds / 1000
        while remaining > 0:
            started = time.monotonic()
            self.abort.wait(min(0.015, remaining))
            remaining -= time.monotonic() - started
            self.check()

    def send(self, events, source_count=0, newline=False):
        self.check()
        # 不跨 SendInput 持有锁；它可能等待主线程上的键盘钩子返回。
        self.win.send(events)
        if source_count:
            with self.control:
                self._sent += source_count
                if newline:
                    self._line += 1
                paused = self._paused
            self.report_progress(force=paused)

    def tap(self, vk, source_count=0, newline=False):
        self.send(key_pair(vk), source_count, newline)
        self.nap(self.cfg["keyDelay"])

    def read_clipboard(self):
        for _ in range(50):
            self.check()
            if self.win.OpenClipboard(None):
                break
            self.nap(10)
        else:
            raise RuntimeError("无法打开剪贴板，请稍后再试")
        try:
            if not self.win.IsClipboardFormatAvailable(13):
                return ""
            handle = self.win.GetClipboardData(13)
            if not handle:
                raise C.WinError(C.get_last_error())
            pointer = self.win.GlobalLock(handle)
            if not pointer:
                raise C.WinError(C.get_last_error())
            try:
                size = self.win.GlobalSize(handle)
                if size < 2:
                    return ""
                raw = C.string_at(pointer, size - size % 2)
                return raw.decode("utf-16-le", errors="surrogatepass").split("\0", 1)[0]
            finally:
                self.win.GlobalUnlock(handle)
        finally:
            self.win.CloseClipboard()

    def run(self):
        result, kind, detail = "", "notice", ""
        try:
            while self.physical.modifiers_down() or self.physical.down(self.trigger):
                self.nap(10)
            self.nap(self.options["start_delay_ms"])
            text = self.read_clipboard()
            if not text:
                result = "剪贴板是空的，或不含文本"
                self._state = "无文本"
                return
            with self.control:
                self._state = "正在输入"
                if not self._paused:
                    self.publish(self.label + "输入中；" + self.shortcut("pause_resume") + " 暂停 / Esc 中止")
            self.type_text(text)
            self.check_cancelled()
            self._state, result = "已完成", self.label + "输入完成"
        except Cancelled as exc:
            self._state, result = "已中止", "已中止：" + str(exc)
        except Exception as exc:
            self._state, result, kind = "输入失败", "输入失败：" + str(exc), "error"
            detail = traceback.format_exc()
        finally:
            with self.control:
                self.finished.set()
                self.armed, self._paused, self._resume_pending = False, False, False
                self.control.notify_all()
                if result:
                    self.publish(result, kind, detail)
            self.notices.put(UiEvent("finished", data=self))

    def type_text(self, text):
        cfg = self.cfg
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        with self.control:
            self.armed = True
            self._total, self._lines = len(text), text.count("\n") + 1
            self._state = "正在输入"
        self.report_progress(force=True)
        for index, line in enumerate(text.split("\n")):
            if index:
                self.tap(VK_RETURN, source_count=1, newline=True)
                if self.options["clear_auto_indent"]:
                    self.send([key_event(VK_SHIFT)] + key_pair(VK_HOME)
                              + [key_event(VK_SHIFT, flags=KEYUP)])
                    self.nap(cfg["keyDelay"])
                    self.tap(VK_DELETE)
                self.nap(cfg["linePause"])
            for block_number, pos in enumerate(range(0, len(line), cfg["chunk"]), 1):
                block = line[pos:pos + cfg["chunk"]]
                if cfg["keyDelay"] <= 0:
                    # keyDelay=0 的快速模式也真正一次发送整个块。
                    self.send(text_events(block), source_count=len(block))
                    if cfg["keyDelay"] == 0:
                        self.nap(0)
                else:
                    for char in block:
                        self.send(text_events(char), source_count=1)
                        self.nap(cfg["keyDelay"])
                self.nap(cfg["pause"])
                if cfg["breatherEvery"] and block_number % cfg["breatherEvery"] == 0:
                    self.nap(cfg["breatherPause"])


class App:
    def __init__(self, win, instance, settings, settings_path, log_path, logger):
        self.win, self.instance = win, instance
        self.settings = copy.deepcopy(settings)
        self.settings_path, self.log_path, self.logger = Path(settings_path), Path(log_path), logger
        self.shutdown, self.physical = threading.Event(), PhysicalKeys()
        self.job, self.worker, self.reload_worker = None, None, None
        self.save_worker, self.pending_settings = None, None
        self.exit_after_save = False
        self.recording, self.record_swallowed = None, set()
        self.hook, self.callback, self.flash, self.tray, self.hotkeys = None, None, None, None, None
        self.reloading, self.error_sticky = False, False
        self.config_service, self.config_open = None, False
        self.last_error, self.last_notice, self.notice_until = "", "", 0
        self.notices = EventBus(win)

    def shortcut(self, action):
        return self.settings["hotkeys"][action]

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def stop(self):
        if self.job:
            self.job.cancel()

    def pause(self, reason="手动暂停"):
        return self.job.pause(reason) if self.busy() and self.job else False

    def resume(self, from_tray=False):
        if self.config_open:
            self.notices.put("请先关闭设置窗口，再回到原输入位置继续")
            return False
        if not self.busy() or not self.job or not self.job.is_paused():
            return False
        job = self.job
        if job.abort.is_set() or self.shutdown.is_set():
            return False
        if job.target:
            if not self.win.IsWindow(job.target):
                job.cancel()
                self.notices.put("无法继续：原目标窗口已关闭")
                return False
            if from_tray:
                if self.win.IsIconic(job.target):
                    self.win.ShowWindow(job.target, 9)
                self.win.SetForegroundWindow(job.target)
            if self.win.GetForegroundWindow() != job.target:
                self.notices.put("仍在暂停：请点击原窗口，再按 " + self.shortcut("pause_resume"))
                return False
        elif from_tray:
            self.notices.put("请先点击需要输入的窗口，再按 " + self.shortcut("pause_resume"))
            return False
        return job.request_resume()

    def toggle_pause(self):
        if not self.busy() or not self.job or self.job.abort.is_set():
            self.notices.put("当前没有输入任务；" + self.shortcut("slow") + " 慢速 / " + self.shortcut("fast") + " 快速开始")
        elif self.job.is_paused() and not self.job.is_resuming():
            self.resume()
        else:
            self.pause()

    def quit(self):
        self.stop()
        self.cancel_recording("录制已取消")
        if self.pending_settings is not None:
            self.exit_after_save = True
            self.notices.put("正在完成配置保存，随后退出")
        else:
            self.shutdown.set()
        if self.tray and self.tray.in_menu:
            self.win.EndMenu()

    def cancel_recording(self, text="录制已取消", label=None):
        if self.recording:
            request, self.recording = self.recording, None
            if self.config_service:
                self.config_service.post("record", dict(request, done=True, text=text, label=label))

    def record_key(self, vk, down):
        # Captured primary keys are swallowed through key-up, including repeats.
        # Modifier events pass through so Windows never retains a stuck modifier.
        if vk in self.record_swallowed:
            if not down:
                self.record_swallowed.discard(vk)
            return True
        if not self.recording:
            return False
        if self.win.GetForegroundWindow() != self.recording["hwnd"]:
            self.cancel_recording("已切换窗口，录制已取消")
            return False
        if vk in MODIFIER_KEYS or not down:
            return False
        self.record_swallowed.add(vk)
        if vk == VK_ESCAPE:
            self.cancel_recording()
            return True
        special = {0x13: "Pause", 0x91: "ScrollLock", 0x2D: "Insert",
                   0x24: "Home", 0x23: "End", 0x21: "PageUp", 0x22: "PageDown"}
        key = (chr(vk) if 65 <= vk <= 90 or 48 <= vk <= 57 else
               "F" + str(vk - 0x6F) if 0x70 <= vk <= 0x87 else special.get(vk))
        modifiers = []
        for label, codes in (("Ctrl", (17, 0xA2, 0xA3)), ("Alt", (18, 0xA4, 0xA5)),
                             ("Shift", (16, 0xA0, 0xA1)), ("Win", (91, 92))):
            if any(self.physical.down(code) for code in codes):
                modifiers.append(label)
        try:
            if key is None:
                raise ConfigError("不支持这个按键，请选择字母、数字、功能键或导航键")
            label = "+".join(modifiers + [key])
            parse_hotkey(self.recording["action"], label)
        except ConfigError as exc:
            self.config_service.post("record", dict(self.recording, done=False, text=str(exc)))
        else:
            self.cancel_recording("已录制 " + label + "；保存后生效", label)
        return True

    def keyboard_hook(self, code, message, pointer):
        # 只维护物理状态、设置暂停/中止和唤醒事件；无日志 I/O、UI 或 Sleep。
        if code == 0 and message in (0x0100, 0x0101, 0x0104, 0x0105):
            key = C.cast(pointer, C.POINTER(KBDLLHOOKSTRUCT)).contents
            down = message in (0x0100, 0x0104)
            if self.physical.observe(key.vkCode, down, injected=bool(key.flags & 0x10)):
                job = self.job
                captured = self.record_key(key.vkCode, down)
                if (not captured and down and key.vkCode == VK_ESCAPE and job
                        and not self.config_open and not (self.tray and self.tray.in_menu)
                        and job.origin_hwnd and self.win.GetForegroundWindow() == job.origin_hwnd):
                    self.stop()
                elif (down and key.vkCode in MODIFIER_KEYS and job and job.armed
                      and job.options["pause_on_modifiers"] and not job.is_paused()):
                    job.pause("检测到手动 Ctrl / Alt / Shift / Win")
                if job:
                    job.notify_key_change()
                if captured:
                    return 1
        return self.win.CallNextHookEx(self.hook, code, message, pointer)

    def start(self, mode):
        if self.busy() or self.shutdown.is_set() or self.config_open or self.exit_after_save:
            return
        foreground = self.win.GetForegroundWindow()
        target = foreground if self.settings["options"]["stop_on_focus_loss"] else None
        if self.settings["options"]["stop_on_focus_loss"] and not target:
            self.notices.put("没有找到目标窗口，请点击输入位置后重试")
            return
        self.error_sticky = False
        key = parse_hotkey(mode, self.shortcut(mode))
        executable = self.win.application_executable(foreground)
        cfg, profile_name = select_speed_profile(self.settings, executable, mode)
        self.flash.set_anchor(foreground)
        self.logger.info("Speed profile=%s; client=%s", profile_name, executable or "unknown")
        self.job = TypingJob(self.win, cfg,
                             "慢速" if mode == "slow" else "快速", key.key, target,
                             self.shutdown, self.notices, self.settings["options"],
                             self.physical, self.shortcut)
        self.job.profile_name = profile_name
        self.job.origin_hwnd = foreground
        self.worker = threading.Thread(target=self.job.run, name="clipboard-typing", daemon=True)
        self.worker.start()

    def on_hotkey(self, hotkey_id):
        if not self.hotkeys or self.shutdown.is_set() or self.recording or self.record_swallowed:
            return
        action = self.hotkeys.actions.get(hotkey_id)
        if action in ("slow", "fast"):
            self.start(action)
        elif action == "pause_resume":
            self.toggle_pause()
        elif action == "stop":
            self.stop()
        elif action == "quit":
            self.quit()

    def reload_settings(self):
        if self.reloading or self.shutdown.is_set() or self.pending_settings is not None:
            return
        self.pause("准备重新加载配置")
        self.reloading = True
        def load():
            try:
                candidate = read_settings(self.settings_path)
                self.notices.put(UiEvent("settings_loaded", data=candidate))
            except Exception as exc:
                self.notices.put(UiEvent("settings_failed", "配置读取失败，原配置继续有效：" + str(exc), traceback.format_exc()))
        self.reload_worker = threading.Thread(target=load, name="settings-loader", daemon=True)
        self.reload_worker.start()

    def apply_settings(self, candidate, persist=False):
        # RegisterHotKey stays on its owning thread; disk writes never run here.
        if self.reloading or self.pending_settings is not None:
            raise ConfigError("正在读取或保存配置，请稍后再试")
        validated = validate_settings(candidate)
        if persist:
            self.hotkeys.stage(validated["hotkeys"])
            self.pending_settings = validated
            def save():
                try:
                    save_settings_atomic(self.settings_path, validated)
                    event = UiEvent("settings_saved")
                except Exception as exc:
                    event = UiEvent("settings_save_failed", str(exc), traceback.format_exc())
                self.notices.put(event)
            self.save_worker = threading.Thread(target=save, name="settings-writer", daemon=True)
            try:
                self.save_worker.start()
            except Exception:
                self.pending_settings = None
                self.hotkeys.rollback()
                raise
            return
        self.hotkeys.apply(validated["hotkeys"])
        self.activate_settings(validated)

    def activate_settings(self, validated):
        self.settings = validated
        if self.job:
            self.job.resume_key = parse_hotkey("pause_resume", self.shortcut("pause_resume")).key
            self.job.notify_key_change()
        self.flash.set_enabled(validated["options"]["show_popup"])

    def hide_popup(self):
        self.flash.hide_by_user()

    def toggle_popup(self):
        if self.flash.is_allowed():
            self.hide_popup()
        else:
            self.flash.restore()
            self.render()

    def open_settings(self):
        if self.shutdown.is_set():
            return
        self.pause("正在调整设置")
        self.config_open = True
        if self.config_service is None:
            def notify(kind, text="", detail="", data=None):
                self.notices.put(UiEvent(kind, text, detail, data))
            self.config_service = ConfigService(self.win, notify, DEFAULT_SETTINGS, validate_settings)
        self.config_service.open(self.settings, self.settings_path)

    def open_text_file(self, path):
        self.pause("正在查看配置或日志")
        try:
            subprocess.Popen(["notepad.exe", str(path)], shell=False)
        except Exception as exc:
            self.notices.put(UiEvent("error", "无法打开文件：" + str(exc), traceback.format_exc()))

    def show_last_error(self):
        if not self.last_error:
            return
        self.pause("正在查看错误")
        self.win.MessageBoxW(self.tray.hwnd, self.last_error + "\n\n完整记录：" + str(self.log_path),
                             "ClipboardTyper 最近错误", 0x10)

    def acknowledge_error(self):
        self.error_sticky = False
        self.last_notice, self.notice_until = "", 0
        self.flash.hide()
        if self.tray:
            snapshot = self.job.snapshot() if self.job else None
            self.tray.set_status(self.progress_text(snapshot) if snapshot and snapshot["total"] else "就绪")

    @staticmethod
    def progress_text(snapshot):
        return (f"已发送 {snapshot['sent']:,}/{snapshot['total']:,} ({snapshot['percent']:.1f}%)"
                f"  第 {snapshot['line']}/{snapshot['lines']} 行  剩余 {snapshot['remaining']:,}")

    def record_error(self, event):
        self.last_error = time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + event.text
        self.error_sticky = True
        self.logger.error("%s\n%s", event.text, event.detail)
        self.pause("发生错误，请查看托盘日志")

    def render(self):
        if not self.tray or not self.flash:
            return
        card_snapshot = self.job.snapshot() if self.job else None
        if card_snapshot and not self.settings["options"]["show_progress"]:
            card_snapshot = dict(card_snapshot, total=0)
        self.flash.set_context(card_snapshot, self.error_sticky, self.shortcut("pause_resume"))
        if self.error_sticky:
            short = self.last_error.split("\n", 1)[-1].replace("\n", " ")
            self.tray.set_status("错误：" + short)
            self.flash.show("错误：" + short[:180] + "\n右键托盘可查看最近错误、打开日志或确认错误", persistent=True)
            return
        snapshot = self.job.snapshot() if self.job else None
        if time.monotonic() < self.notice_until and self.last_notice:
            heading = self.last_notice
        elif snapshot:
            heading = snapshot["label"] + " · " + snapshot["state"] + " · " + snapshot["profile_name"]
            if snapshot["reason"] and snapshot["state"] == "已暂停":
                heading += "：" + snapshot["reason"]
        else:
            heading = "就绪；" + self.shortcut("slow") + " 慢速 / " + self.shortcut("fast") + " 快速"
        summary = self.progress_text(snapshot) if snapshot and snapshot["total"] else ""
        self.tray.set_status(heading + (" | " + summary if summary else ""))
        show_progress = self.settings["options"]["show_progress"] and bool(summary)
        message = heading + ("\n" + summary if show_progress else "")
        active = self.busy() and self.job is not None and not self.job.finished.is_set()
        self.flash.show(message, self.settings["options"]["notice_duration_ms"],
                        persistent=active and show_progress)

    def handle_events(self):
        dirty = False
        for value in self.notices.drain():
            event = value if isinstance(value, UiEvent) else UiEvent("notice", str(value))
            if isinstance(event.data, TypingJob) and event.data is not self.job:
                if event.kind == "error":
                    self.logger.error("Previous task: %s\n%s", event.text, event.detail)
                continue
            if event.kind == "gui_closed":
                self.cancel_recording()
                self.config_open = False
                continue
            if event.kind == "record_start":
                self.cancel_recording()
                if self.config_open and self.pending_settings is None and not self.shutdown.is_set():
                    self.recording = dict(event.data)
                    self.config_service.post("record", dict(self.recording, done=False,
                                                            text="请按组合键；Esc 取消（15 秒内）"))
                continue
            if event.kind == "record_cancel":
                if self.recording and self.recording["token"] == event.data:
                    self.cancel_recording()
                continue
            if event.kind == "gui_save":
                if self.shutdown.is_set():
                    continue
                try:
                    if self.reloading:
                        raise ConfigError("正在重新加载配置，请稍后再保存")
                    self.cancel_recording()
                    self.apply_settings(event.data, persist=True)
                except Exception as exc:
                    self.config_service.post("result", {"ok": False, "error": str(exc)})
                    self.logger.exception("Settings GUI save failed")
                continue
            if event.kind in ("settings_saved", "settings_save_failed"):
                candidate, self.pending_settings = self.pending_settings, None
                if event.kind == "settings_saved":
                    self.hotkeys.commit()
                    self.activate_settings(candidate)
                    result = {"ok": True, "settings": self.settings}
                    event = UiEvent("notice", "设置已保存并应用；速度与输入保护从下一次任务生效")
                else:
                    self.hotkeys.rollback()
                    result = {"ok": False, "error": event.text}
                    event = UiEvent("error", "配置保存失败，原配置继续有效：" + event.text, event.detail)
                if self.config_service:
                    self.config_service.post("result", result)
                if self.exit_after_save:
                    self.shutdown.set()
            elif event.kind in ("gui_error", "gui_failed", "card_error"):
                if event.kind == "gui_failed":
                    self.cancel_recording()
                    self.config_open = False
                    self.config_service = None
                event.kind = "error"
            if event.kind == "settings_loaded":
                self.reloading = False
                if self.shutdown.is_set():
                    continue
                try:
                    self.apply_settings(event.data)
                    event = UiEvent("notice", "配置已加载：快捷键立即生效；速度和输入选项从下一次任务生效")
                except Exception as exc:
                    event = UiEvent("error", "配置未应用，原配置继续有效：" + str(exc), traceback.format_exc())
            elif event.kind == "settings_failed":
                self.reloading = False
                event.kind = "error"
            if event.kind == "error":
                self.record_error(event)
            elif event.kind == "notice":
                self.last_notice = event.text
                self.notice_until = time.monotonic() + self.settings["options"]["notice_duration_ms"] / 1000
                self.logger.info(event.text)
            dirty = True
        if dirty:
            self.render()

    def pump(self):
        message = MSG()
        while self.win.PeekMessageW(C.byref(message), None, 0, 0, 1):
            if message.message == WM_QUIT:
                self.quit()
            elif message.message == WM_HOTKEY and not message.hwnd:
                self.on_hotkey(message.wParam)
            else:
                self.win.TranslateMessage(C.byref(message))
                self.win.DispatchMessageW(C.byref(message))

    def wait_timeout(self):
        deadlines = []
        if self.flash and self.flash.deadline:
            deadlines.append(self.flash.deadline)
        if self.tray and not self.tray.added:
            deadlines.append(self.tray.next_retry)
        if not deadlines:
            return INFINITE
        return min(INFINITE - 1, max(0, math.ceil((min(deadlines) - time.monotonic()) * 1000)))

    def wait_for_work(self, timeout=None, exiting=False):
        values = [self.notices.handle]
        if not exiting and not self.exit_after_save:
            values.insert(0, self.instance.event)
        handles = (HANDLE * len(values))(*values)
        result = self.win.MsgWaitForMultipleObjectsEx(
            len(values), handles, self.wait_timeout() if timeout is None else timeout,
            0x04FF, 0x0004)  # QS_ALLINPUT | MWMO_INPUTAVAILABLE
        if result == WAIT_FAILED:
            raise C.WinError(C.get_last_error())

    def run(self, initial_error=None):
        try:
            self.flash = StatusCard(self.win, self.settings["options"]["show_popup"],
                                    menu_callback=lambda: self.tray.show_menu(), hide_callback=self.hide_popup,
                                    error_callback=lambda text, detail: self.notices.put(UiEvent("card_error", text, detail)))
            self.tray = TrayIcon(self)
            self.tray.open()
            self.hotkeys = HotkeyManager(self.win, self.tray.hwnd)
            self.callback = self.win.HOOKPROC(self.keyboard_hook)
            self.hook = self.win.SetWindowsHookExW(13, self.callback, self.win.GetModuleHandleW(None), 0)
            if not self.hook:
                raise C.WinError(C.get_last_error())
            self.physical.seed(self.win, (parse_hotkey(action, value).key
                                          for action, value in self.settings["hotkeys"].items()))
            self.hotkeys.apply(self.settings["hotkeys"])
            self.notices.put("已启动：" + self.shortcut("slow") + " 慢速 / " + self.shortcut("fast")
                             + " 快速；" + self.shortcut("pause_resume") + " 暂停/继续")
            if initial_error:
                self.notices.put(initial_error)
            while not self.shutdown.is_set():
                try:
                    self.pump()
                    if not self.exit_after_save and self.instance.replacement_requested():
                        self.quit()
                    self.handle_events()
                    self.flash.tick()
                    self.tray.tick()
                    if not self.shutdown.is_set():
                        self.wait_for_work()
                except KeyboardInterrupt:
                    self.quit()
        finally:
            self.quit()
            # 退出仍泵送消息，保证 SendInput 等待的钩子可以完成。
            deadline = time.monotonic() + 1
            while self.busy() and time.monotonic() < deadline:
                self.pump()
                self.handle_events()
                self.wait_for_work(timeout=50, exiting=True)
            if self.hook:
                self.win.UnhookWindowsHookEx(self.hook)
            if self.config_service:
                self.config_service.close()
            if self.hotkeys:
                self.hotkeys.close()
            if self.tray:
                self.tray.close()
            if self.flash:
                self.flash.close()
            self.notices.close()
            self.logger.info("Program exited")


def main():
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
        settings_path, log_path = application_paths()
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
