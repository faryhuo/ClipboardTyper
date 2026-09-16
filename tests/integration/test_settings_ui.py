"""Run real Windows/Tk regression scenarios in a child process.

A Tk/native callback crash aborts Python, so it cannot be caught by unittest.
The child uses temporary settings and never registers global hotkeys.
"""
import copy
import ctypes
import logging
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from clipboard_typer.services import application as app_module
from clipboard_typer.ui import settings as ui
from clipboard_typer.core.config import DEFAULT_SETTINGS, read_settings, save_settings_atomic, validate_settings
from clipboard_typer.platforms.windows import Win32


def run_scenario():
    ctypes.windll.kernel32.SetErrorMode(0x0002)
    results = queue.Queue()
    errors = queue.Queue()
    ui_threads = set()

    class TestEditor(ui.SettingsEditor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.round = 0
            # Exercise actual Tk widgets without taking the user's focus.
            self.root.deiconify = lambda: None
            self.root.lift = lambda: None
            self.root.focus_force = lambda: None

        def open(self, *args):
            super().open(*args)
            import threading
            ui_threads.add(threading.get_ident())
            self.round += 1
            variable = self.variables[("profiles", "slow", "keyDelay")]
            if self.round == 1:
                variable.set("invalid")
                self.save_button.invoke()
                assert not self.saving, "Invalid input must not start a save"
            variable.set(str(10 + self.round))
            # Exercise the button callback, not just the persistence function.
            self.root.after(10, self.save_button.invoke)

        def save_result(self, result):
            super().save_result(result)
            assert not self.saving
            assert not self.save_button.instate(["disabled"])
            self.close()
            assert not self.visible
            results.put((self.round, copy.deepcopy(result)))

    with tempfile.TemporaryDirectory() as directory:
        settings_path = Path(directory) / "settings.json"
        original = copy.deepcopy(DEFAULT_SETTINGS)
        save_settings_atomic(settings_path, original)
        logger = logging.getLogger("settings-ui-regression")
        logger.addHandler(logging.NullHandler())
        win = Win32()
        app = app_module.App(win, None, original, settings_path,
                             Path(directory) / "test.log", logger)
        native_hotkeys = Mock()
        native_hotkeys.RegisterHotKey.return_value = True
        app.hotkeys = app_module.HotkeyManager(native_hotkeys, None)
        app.hotkeys.apply(original["hotkeys"])
        app.flash = Mock()
        app.render = lambda: None
        writes = 0
        real_save = app_module.save_settings_atomic

        def save(path, settings):
            nonlocal writes
            writes += 1
            if writes == 11:
                raise PermissionError("Simulated read-only settings file")
            real_save(path, settings)

        def notify(kind, text="", detail="", data=None):
            if kind in ("gui_failed", "gui_error"):
                errors.put((text, detail))
            app.notices.put(app_module.UiEvent(kind, text, detail, data))

        service = ui.ConfigService(win, notify, original, validate_settings)
        app.config_service = service
        previous = original
        completed = 0
        with patch.object(ui, "SettingsEditor", TestEditor), \
                patch.object(app_module, "save_settings_atomic", save):
            try:
                app.open_settings()
                # Allow slower CI desktops to finish all 30 real Tk cycles.
                deadline = time.monotonic() + 60
                while completed < 30 and time.monotonic() < deadline:
                    app.handle_events()
                    assert errors.empty(), list(errors.queue)
                    assert not app.shutdown.is_set(), "Saving must not exit the app"
                    assert service.thread.is_alive(), "Settings UI thread exited"
                    try:
                        number, result = results.get(timeout=0.01)
                    except queue.Empty:
                        continue
                    completed = number
                    persisted = read_settings(settings_path)
                    if number == 11:
                        assert not result["ok"]
                        assert "read-only" in result["error"]
                        assert persisted == previous == app.settings
                    else:
                        assert result["ok"], result
                        assert persisted == app.settings == result["settings"]
                        assert persisted["profiles"]["slow"]["keyDelay"] == 10 + number
                    assert app.hotkeys.pending is None
                    assert app.pending_settings is None
                    previous = copy.deepcopy(app.settings)
                    if completed < 30:
                        app.open_settings()
                assert completed == 30, f"Only {completed} saves completed"
            finally:
                service.close()
                app.hotkeys.close()
                app.notices.close()
            assert not service.thread.is_alive(), "Settings UI did not shut down"
            assert errors.empty(), list(errors.queue)
            assert len(ui_threads) == 1, "Reopening should reuse the Tk thread"
    print("PASS: 29 saves, disk failure rollback, validation, reopen, shutdown", flush=True)


@unittest.skipUnless(sys.platform == "win32", "Requires Windows and Tcl/Tk")
class SettingsUIRegression(unittest.TestCase):
    def test_save_and_reopen_in_real_tk(self):
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", str(Path(__file__).resolve()), "--scenario"],
            capture_output=True, text=True, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS:", result.stdout)


if __name__ == "__main__":
    if "--scenario" in sys.argv:
        run_scenario()
    else:
        unittest.main()
