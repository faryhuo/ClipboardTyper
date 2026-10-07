import json
from pathlib import Path

import pytest

from clipboard_typer.core.config import (
    ConfigError,
    DEFAULT_SETTINGS,
    OPTION_RANGES,
    PROFILE_RANGES,
    SPECIAL_KEYS,
    key_label,
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
    {"options": {"max_file_kib": 0}},
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


def test_bundled_settings_file_matches_defaults():
    bundled = Path(__file__).resolve().parents[2] / "settings.json"
    assert json.loads(bundled.read_text(encoding="utf-8")) == DEFAULT_SETTINGS


@pytest.mark.parametrize("label", ["A", "Z", "0", "9", "F1", "F11", "F13", "F24", *SPECIAL_KEYS])
def test_recorded_key_labels_round_trip_through_parser(label):
    hotkey = parse_hotkey("slow", "Ctrl+" + label)
    assert key_label(hotkey.key) == label


@pytest.mark.parametrize("vk", [0x1B, 0x20, 0x60, 0xBA])
def test_unsupported_keys_have_no_label(vk):
    assert key_label(vk) is None


def test_settings_editor_uses_validation_ranges():
    from clipboard_typer.ui.settings import NUMERIC_FIELDS, PROFILE_FIELDS
    assert {key: (lo, hi) for key, _, _, lo, hi in PROFILE_FIELDS} == PROFILE_RANGES
    assert {key: (lo, hi) for key, _, _, lo, hi in NUMERIC_FIELDS} == OPTION_RANGES
