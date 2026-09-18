import ctypes as C
import base64
import queue
import threading
from unittest.mock import Mock

import pytest

from clipboard_typer.core.config import ConfigError
from clipboard_typer.platforms.windows import CF_HDROP, CF_UNICODETEXT, KEYUP, UNICODE, VK_CONTROL, VK_RETURN, text_events
from clipboard_typer.services.hotkeys import HotkeyManager, PhysicalKeys
from clipboard_typer.services.typing_service import (
    BASE64_LINE_LENGTH,
    FILE_BEGIN_PREFIX,
    FILE_BEGIN_SUFFIX,
    FILE_END,
    Cancelled,
    TypingJob,
)


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
    assert [len(batch) for batch in batches] == [2, 2, 2, 4, 2, 2]
    assert batches[4][0].ki.wVk == VK_RETURN
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


@pytest.mark.parametrize("delay", [-1, 0, 1, 10, 25])
@pytest.mark.parametrize("chunk", [1, 12, 256])
def test_mixed_text_survives_a_delayed_unicode_receiver(job, monkeypatch, delay, chunk):
    # Model a client that translates queued VK_PACKET messages using the most
    # recently submitted UTF-16 unit. Bursting distinct characters corrupts it.
    pending, received = [], bytearray()
    elapsed = 0
    job.cfg.update(keyDelay=delay, chunk=chunk)

    def receive(events):
        nonlocal elapsed
        units = [event.ki.wScan for event in events if not event.ki.dwFlags & KEYUP]
        # A supplementary character is one atomic UTF-16 pair.
        if len(units) == 2 and 0xD800 <= units[0] <= 0xDBFF and 0xDC00 <= units[1] <= 0xDFFF:
            packet = b"".join(unit.to_bytes(2, "little") for unit in units)
            pending.append(packet)
        else:
            pending.extend(unit.to_bytes(2, "little") for unit in units)
        elapsed = 0

    def advance(milliseconds):
        nonlocal elapsed
        elapsed += max(0, milliseconds)
        if elapsed >= 10 and pending:
            received.extend(pending[-1] * len(pending))
            pending.clear()

    job.win.send.side_effect = receive
    monkeypatch.setattr(job, "nap", advance)
    source = "项目采用 src 布局，安装包运行。中文复制：甲乙丙丁 | core/config.py `配置` 😀𠀀"
    job.type_text(source)
    assert not pending
    assert received.decode("utf-16-le") == source
    assert job.snapshot()["sent"] == job.snapshot()["total"] == len(source)


def test_cancel_between_characters_stops_inside_a_large_block(job, monkeypatch):
    job.cfg["chunk"] = 256
    real_nap = job.nap

    def cancel_during_delay(milliseconds):
        job.cancel()
        real_nap(milliseconds)

    monkeypatch.setattr(job, "nap", cancel_during_delay)
    with pytest.raises(Cancelled):
        job.type_text("中文复制不应重复")
    assert job.snapshot()["sent"] == 1
    job.win.send.assert_called_once()


def test_failure_mid_block_counts_only_successful_characters(job):
    job.cfg["chunk"] = 12
    job.win.send.side_effect = [None, RuntimeError("SendInput failed")]
    with pytest.raises(RuntimeError, match="SendInput"):
        job.type_text("甲乙丙")
    assert job.snapshot()["sent"] == 1
    assert job.win.send.call_count == 2


def test_clipboard_preserves_utf16_and_stops_at_terminator(job):
    source = "中文复制\r\n架构说明 😀𠀀 café"
    data = (source + "\0不应读取").encode("utf-16-le")
    buffer = C.create_string_buffer(data)
    job.win.IsClipboardFormatAvailable.side_effect = lambda format_id: format_id == CF_UNICODETEXT
    job.win.GlobalLock.return_value = C.addressof(buffer)
    job.win.GlobalSize.return_value = len(data)
    assert job.read_clipboard() == source
    assert [call.args[0] for call in job.win.IsClipboardFormatAvailable.call_args_list] == [
        CF_HDROP, CF_UNICODETEXT,
    ]
    job.win.GlobalUnlock.assert_called_once()
    job.win.CloseClipboard.assert_called_once()


def test_clipboard_file_contents_take_priority_over_path_text(job, tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "image.png"
    first_data = ("第一份\n" * 20).encode("utf-8")
    second_data = b"\x89PNG\r\n\x1a\n\x00\xff"
    first.write_bytes(first_data)
    second.write_bytes(second_data)
    job.win.IsClipboardFormatAvailable.side_effect = lambda format_id: format_id in {
        CF_HDROP, CF_UNICODETEXT,
    }
    job.win.GetClipboardData.return_value = 42
    paths = [str(first), str(second)]

    def query_file(_handle, index, buffer, _size):
        if index == 0xFFFFFFFF:
            return len(paths)
        value = paths[index]
        if buffer is None:
            return len(value)
        buffer.value = value
        return len(value)

    job.win.DragQueryFileW.side_effect = query_file

    first_payload = base64.b64encode(first_data).decode("ascii")
    first_payload = "\n".join(
        first_payload[offset:offset + BASE64_LINE_LENGTH]
        for offset in range(0, len(first_payload), BASE64_LINE_LENGTH)
    )
    assert job.read_clipboard() == (
        f"{FILE_BEGIN_PREFIX}first.txt{FILE_BEGIN_SUFFIX}\n"
        f"{first_payload}\n{FILE_END}\n"
        f"{FILE_BEGIN_PREFIX}image.png{FILE_BEGIN_SUFFIX}\n"
        f"{base64.b64encode(second_data).decode('ascii')}\n{FILE_END}"
    )
    job.win.GlobalLock.assert_not_called()
    job.win.CloseClipboard.assert_called_once()


def test_clipboard_directory_is_not_typed_as_its_path(job, tmp_path):
    job.win.IsClipboardFormatAvailable.side_effect = lambda format_id: format_id == CF_HDROP
    job.win.GetClipboardData.return_value = 42
    path = str(tmp_path)

    def query_file(_handle, index, buffer, _size):
        if index == 0xFFFFFFFF:
            return 1
        if buffer is None:
            return len(path)
        buffer.value = path
        return len(path)

    job.win.DragQueryFileW.side_effect = query_file

    with pytest.raises(RuntimeError, match="不是普通文件"):
        job.read_clipboard()
    job.win.CloseClipboard.assert_called_once()
