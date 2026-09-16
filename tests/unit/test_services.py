import queue
import threading
from unittest.mock import Mock

import pytest

from clipboard_typer.core.config import ConfigError
from clipboard_typer.platforms.windows import KEYUP, UNICODE, VK_CONTROL, VK_RETURN, text_events
from clipboard_typer.services.hotkeys import HotkeyManager, PhysicalKeys
from clipboard_typer.services.typing_service import Cancelled, TypingJob


def test_hotkey_registration_failure_rolls_back_new_bindings():
    win = Mock()
    win.RegisterHotKey.return_value = True
    manager = HotkeyManager(win, None)
    manager.apply({"slow": "Ctrl+J"})
    old_bindings = dict(manager.bindings)
    old_actions = dict(manager.actions)
    win.RegisterHotKey.side_effect = [True, False]
    with pytest.raises(ConfigError):
        manager.apply({"slow": "Ctrl+K", "fast": "Ctrl+L"})
    assert manager.bindings == old_bindings
    assert manager.actions == old_actions
    assert manager.pending is None
    win.UnregisterHotKey.assert_called_once_with(None, 2)


def test_hotkey_stage_keeps_old_binding_until_commit():
    win = Mock()
    win.RegisterHotKey.return_value = True
    manager = HotkeyManager(win, None)
    manager.apply({"slow": "Ctrl+J"})
    manager.stage({"slow": "Ctrl+K"})
    assert (2, ord("J")) in manager.bindings
    win.UnregisterHotKey.assert_not_called()
    manager.commit()
    assert (2, ord("K")) in manager.bindings
    assert (2, ord("J")) not in manager.bindings
    win.UnregisterHotKey.assert_called_once_with(None, 1)


def test_injected_modifiers_do_not_pause_typing():
    physical = PhysicalKeys()
    assert not physical.observe(VK_CONTROL, True, injected=True)
    assert not physical.modifiers_down()
    physical.observe(VK_CONTROL, True)
    assert physical.modifiers_down()
    physical.observe(VK_CONTROL, False)
    assert not physical.modifiers_down()


def test_unicode_surrogate_pairs_are_sent_as_down_up_events():
    events = text_events("中😀")
    assert len(events) == 6
    assert [event.ki.wScan for event in events[::2]] == [0x4E2D, 0xD83D, 0xDE00]
    assert all(event.ki.dwFlags == UNICODE for event in events[::2])
    assert all(event.ki.dwFlags == UNICODE | KEYUP for event in events[1::2])


@pytest.fixture
def job(settings):
    options = settings["options"]
    options["clear_auto_indent"] = False
    profile = dict(keyDelay=-1, chunk=2, pause=0, breatherEvery=0, breatherPause=0, linePause=0)
    return TypingJob(Mock(), profile, "fast", 0, None, threading.Event(), queue.SimpleQueue(), options=options)


def test_typing_chunks_normalize_newlines_and_count_source_characters(job):
    job.type_text("ab中😀\r\nx")
    batches = [call.args[0] for call in job.win.send.call_args_list]
    assert [len(batch) for batch in batches] == [4, 6, 2, 2]
    assert batches[2][0].ki.wVk == VK_RETURN
    snapshot = job.snapshot()
    assert snapshot["sent"] == snapshot["total"] == 6
    assert snapshot["line"] == snapshot["lines"] == 2
    assert snapshot["remaining"] == 0


def test_failed_send_does_not_advance_progress(job):
    job.win.send.side_effect = RuntimeError("SendInput failed")
    with pytest.raises(RuntimeError, match="SendInput"):
        job.type_text("abc")
    assert job.snapshot()["sent"] == 0


def test_paused_job_resumes_and_cancel_prevents_further_input(job):
    assert job.pause()
    assert job.is_paused()
    assert job.request_resume()
    job.check()
    assert not job.is_paused()
    job.cancel()
    with pytest.raises(Cancelled):
        job.type_text("abc")
    job.win.send.assert_not_called()
