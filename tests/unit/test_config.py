import json

import pytest

from clipboard_typer.core.config import (
    ConfigError,
    DEFAULT_SETTINGS,
    parse_hotkey,
    read_settings,
    save_settings_atomic,
    validate_settings,
)
from clipboard_typer.services.profiles import select_speed_profile


def test_legacy_settings_fill_defaults_without_mutating_input():
    legacy = {"profiles": {"fast": {"chunk": 24}}}
    result = validate_settings(legacy)
    assert result["profiles"]["fast"]["chunk"] == 24
    assert result["remote_desktop"] == DEFAULT_SETTINGS["remote_desktop"]
    result["remote_desktop"]["executables"].append("other.exe")
    assert "other.exe" not in DEFAULT_SETTINGS["remote_desktop"]["executables"]
    assert legacy == {"profiles": {"fast": {"chunk": 24}}}


@pytest.mark.parametrize("raw", [
    [], {"unknown": 1}, {"version": True},
    {"profiles": {"fast": {"chunk": 0}}},
    {"options": {"start_delay_ms": True}},
    {"hotkeys": {"fast": "Ctrl+J"}},
    {"remote_desktop": {"executables": ["C:\\mstsc.exe"]}},
])
def test_invalid_settings_rejected(raw):
    with pytest.raises(ConfigError):
        validate_settings(raw)


@pytest.mark.parametrize("value", ["J", "Shift+J", "F12", "Esc", "Ctrl+Ctrl+J", "Ctrl+"])
def test_unsafe_or_invalid_hotkeys_rejected(value):
    with pytest.raises(ConfigError):
        parse_hotkey("slow", value)


def test_hotkey_aliases_have_same_combo():
    assert parse_hotkey("slow", "control+j").combo == parse_hotkey("fast", "Ctrl+J").combo


def test_atomic_save_roundtrip(tmp_path, settings):
    path = tmp_path / "settings.json"
    save_settings_atomic(path, settings)
    assert read_settings(str(path)) == settings


def test_atomic_save_failure_preserves_file_and_cleans_temp(tmp_path, settings, monkeypatch):
    path = tmp_path / "settings.json"
    save_settings_atomic(path, settings)
    previous = path.read_bytes()
    settings["profiles"]["fast"]["chunk"] = 24

    def fail_replace(*args):
        raise PermissionError("read-only")

    monkeypatch.setattr("clipboard_typer.core.config.os.replace", fail_replace)
    with pytest.raises(PermissionError):
        save_settings_atomic(path, settings)
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


def test_read_bom_and_reject_oversized_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({}), encoding="utf-8-sig")
    assert read_settings(path) == DEFAULT_SETTINGS
    path.write_bytes(b" " * 65537)
    with pytest.raises(ConfigError, match="64 KiB"):
        read_settings(path)


@pytest.mark.parametrize("executable,label", [
    (r"C:\Windows\System32\MSTSC.EXE", "远程桌面"),
    ("CDViewer.exe", "Citrix Workspace"),
    ("notepad.exe", "通用"),
])
def test_speed_profile_selection(settings, executable, label):
    profile, actual = select_speed_profile(settings, executable, "fast")
    assert actual == label
    source = settings["profiles"] if label == "通用" else settings["remote_desktop"]["profiles"]
    assert profile == source["fast"]
    profile["chunk"] = 123
    assert source["fast"]["chunk"] != 123


def test_remote_override_can_be_disabled(settings):
    settings["remote_desktop"]["enabled"] = False
    profile, label = select_speed_profile(settings, "mstsc.exe", "fast")
    assert label == "通用"
    assert profile == settings["profiles"]["fast"]
