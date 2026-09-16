"""Configuration defaults, validation and atomic JSON persistence."""
from dataclasses import dataclass
from pathlib import Path
import copy
import json
import os
import tempfile


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

def read_settings(path):
    path = Path(path)
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
