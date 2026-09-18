"""Hotkey task handoffs use fake Windows input and never type into the desktop."""
import threading
import time
from unittest.mock import Mock

import pytest

from clipboard_typer.core.config import parse_hotkey
from clipboard_typer.platforms.windows import INFINITE, VK_CONTROL
from clipboard_typer.services.application import App
from clipboard_typer.services.hotkeys import HotkeyManager
from clipboard_typer.services.typing_service import TypingJob
from clipboard_typer.ui.status_card import card_scene


@pytest.fixture
def app(settings, tmp_path):
    settings["options"].update(start_delay_ms=0, clear_auto_indent=False)
    for profile in settings["profiles"].values():
        profile.update(keyDelay=-1, chunk=2, pause=0, breatherEvery=0, linePause=0)
    win = Mock()
    win.GetForegroundWindow.return_value = 123
    win.application_executable.return_value = "editor.exe"
    result = App(win, None, settings, tmp_path / "settings.json", tmp_path / "log", Mock())
    result.flash = Mock(deadline=0)
    result.hotkeys = HotkeyManager(win, None)
    result.hotkeys.apply(settings["hotkeys"])
    result.job = TypingJob(win, settings["profiles"]["slow"], "慢速", ord("J"), 123,
                           result.shutdown, result.notices, settings["options"], result.physical)
    result.worker = Mock()
    result.worker.is_alive.return_value = True
    yield result
    result.stop()
    if isinstance(result.worker, threading.Thread):
        result.worker.join(timeout=2)
        assert not result.worker.is_alive()
    result.notices.close()


def press(app, action):
    key = parse_hotkey(action, app.shortcut(action))
    app.on_hotkey(app.hotkeys.bindings[key.combo])


def notices(app):
    return [event if isinstance(event, str) else event.text for event in app.notices.drain()]


@pytest.mark.parametrize("mode,label", [("slow", "慢速"), ("fast", "快速")])
def test_paused_hotkey_replaces_job_and_reads_new_clipboard(app, monkeypatch, mode, label):
    old = app.job
    old.type_text("old")
    old.pause()
    read = Mock(return_value="new clipboard")
    monkeypatch.setattr(TypingJob, "read_clipboard", read)
    press(app, mode)
    assert old.abort.is_set()
    assert app.pending_start == (mode, 123)
    app.start_pending()
    assert app.job is old  # No overlapping workers, even if the old job reports finished.
    old.finished.set()
    app.start_pending()
    assert app.job is old
    assert 0 <= app.wait_timeout() < INFINITE

    app.worker.is_alive.return_value = False
    app.start_pending()
    app.worker.join(timeout=2)
    assert not app.worker.is_alive()
    assert app.job is not old
    assert app.job.label == label
    assert app.job.snapshot()["sent"] == len("new clipboard")
    assert old.snapshot()["sent"] == len("old")
    assert app.pending_start is None
    read.assert_called_once()
    assert app.wait_timeout() == INFINITE


def test_f8_keeps_paused_job_and_progress(app):
    old = app.job
    old.type_text("old")
    old.pause()
    press(app, "pause_resume")
    assert app.job is old
    assert old.is_resuming()
    assert not old.abort.is_set()
    assert app.pending_start is None
    assert old.snapshot()["sent"] == 3


@pytest.mark.parametrize("action", ["stop", "quit"])
def test_stop_or_quit_cancels_queued_replacement(app, action):
    app.job.pause()
    press(app, "slow")
    press(app, action)
    app.worker.is_alive.return_value = False
    old = app.job
    app.start_pending()
    assert app.pending_start is None
    assert app.job is old


def test_focus_change_during_handoff_requires_another_hotkey(app):
    app.job.pause()
    press(app, "slow")
    app.worker.is_alive.return_value = False
    app.win.GetForegroundWindow.return_value = 456
    old = app.job
    app.start_pending()
    assert app.job is old
    assert app.pending_start is None
    assert any("目标窗口已切换" in text for text in notices(app))


@pytest.mark.parametrize("mode", ["slow", "fast"])
@pytest.mark.parametrize("state,message", [
    ("running", "当前任务正在输入"),
    ("stopping", "正在中止当前任务"),
    ("settings", "请先关闭设置窗口"),
])
def test_unavailable_start_explains_why(app, mode, state, message):
    old = app.job
    if state == "stopping":
        old.cancel()
    elif state == "settings":
        app.config_open = True
    press(app, mode)
    assert app.job is old
    assert app.pending_start is None
    assert any(message in text for text in notices(app))


def test_held_start_keys_publish_waiting_message(app, monkeypatch):
    app.physical.observe(VK_CONTROL, True)
    app.physical.observe(ord("J"), True)
    read = Mock(return_value="new")
    monkeypatch.setattr(app.job, "read_clipboard", read)

    def release_after_notice(milliseconds):
        assert app.job.snapshot()["state"] == "等待松开快捷键"
        assert any("请松开快捷键" in text for text in notices(app))
        read.assert_not_called()
        app.physical.observe(VK_CONTROL, False)
        app.physical.observe(ord("J"), False)
        monkeypatch.setattr(app.job, "nap", lambda milliseconds: None)

    monkeypatch.setattr(app.job, "nap", release_after_notice)
    app.job.run()
    read.assert_called_once()
    assert app.job.snapshot()["sent"] == 3


@pytest.mark.parametrize("action", ["slow", "fast", "pause_resume"])
def test_settings_block_notice_reaches_editor_and_card_with_existing_progress(app, action):
    app.job.type_text("old text")
    app.job.pause()
    app.config_open = True
    app.config_service = Mock()
    app.tray = Mock()
    press(app, action)
    message = app.config_service.post.call_args.args[1]
    app.config_service.post.assert_called_once_with("notice", message)
    app.handle_events()
    args, kwargs = app.flash.set_context.call_args
    scene = card_scene(app.flash.show.call_args.args[0], *args, **kwargs)
    assert any(op[0] == "text" and op[2] == message for op in scene)
    assert any(op[0] == "text" and op[2] == "操作提示" for op in scene)
    assert app.wait_timeout() < INFINITE
    app.notice_until = 0
    app.render()
    assert not app.flash.set_context.call_args.kwargs["notice"]


@pytest.mark.parametrize("mode", ["slow", "fast"])
def test_live_paused_worker_exits_and_replacement_completes(app, monkeypatch, mode):
    old = app.job
    paused = threading.Event()
    reads = Mock(side_effect=["old clipboard", "new clipboard"])
    monkeypatch.setattr(TypingJob, "read_clipboard", reads)

    def pause_first_send(events):
        if not paused.is_set():
            old.pause()
            paused.set()

    app.win.send.side_effect = pause_first_send
    app.worker = threading.Thread(target=old.run, daemon=True)
    app.worker.start()
    assert paused.wait(timeout=2)
    press(app, mode)
    deadline = time.monotonic() + 2
    while app.pending_start is not None and time.monotonic() < deadline:
        app.worker.join(timeout=0.01)
        app.start_pending()
    app.worker.join(timeout=2)
    assert app.pending_start is None
    assert app.job is not old
    assert old.finished.is_set()
    assert old.snapshot()["sent"] < len("old clipboard")
    assert app.job.snapshot()["sent"] == len("new clipboard")
    assert reads.call_count == 2
