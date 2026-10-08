"""Clipboard typing jobs, progress and pause/resume behavior."""
import base64
import copy
import ctypes as C
import threading
import time
import traceback
from pathlib import Path
from clipboard_typer.core.config import DEFAULT_SETTINGS, parse_hotkey
from clipboard_typer.core.events import UiEvent
from clipboard_typer.platforms.windows import CF_HDROP, CF_UNICODETEXT, KEYUP, VK_DELETE, VK_HOME, VK_RETURN, VK_SHIFT, key_event, key_pair, text_events
from clipboard_typer.services.hotkeys import PhysicalKeys


# Leave time for the target to translate VK_PACKET before submitting the next
# character. A whole Unicode block can repeat/drop characters in some clients.
MIN_KEY_DELAY_MS = 10
FILE_BEGIN_PREFIX = "<<<CLIPBOARD_TYPER_FILE_BEGIN:"
TEXT_BEGIN_PREFIX = "<<<CLIPBOARD_TYPER_TEXT_BEGIN:"
FILE_BEGIN_SUFFIX = ">>>"
FILE_END = "<<<CLIPBOARD_TYPER_FILE_END>>>"
BASE64_LINE_LENGTH = 76
# Only images are Base64-encoded; other files are typed as their text.
IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".gif", ".bmp", ".dib", ".webp",
    ".ico", ".cur", ".tif", ".tiff", ".heic", ".heif", ".avif", ".svg", ".emf", ".wmf",
})
_TEXT_CONTROLS = frozenset("\t\n\r\f")
# Estimate only after enough input to smooth out start-up and line pauses.
ETA_MIN_SECONDS, ETA_MIN_CHARACTERS = 3, 20


def format_eta(seconds):
    seconds = max(1, round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"约剩 {hours} 小时 {minutes} 分"
    if minutes:
        return f"约剩 {minutes} 分 {seconds} 秒"
    return f"约剩 {seconds} 秒"


def decode_text_file(data):
    """Return the text of a text file, or None if it looks binary."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    elif b"\0" in data:
        return None
    else:
        encodings = ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if any(char < " " and char not in _TEXT_CONTROLS for char in text):
            return None
        return text
    return None


def base64_block(name, data):
    encoded = base64.b64encode(data).decode("ascii")
    payload = "\n".join(
        encoded[offset:offset + BASE64_LINE_LENGTH]
        for offset in range(0, len(encoded), BASE64_LINE_LENGTH)
    )
    return f"{FILE_BEGIN_PREFIX}{name}{FILE_BEGIN_SUFFIX}\n{payload}\n{FILE_END}"


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
        # Typing time excluding pauses, for the remaining-time estimate.
        self._active_seconds, self._active_since = 0.0, None
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

    def _set_state(self, state):
        with self.control:
            self._state = state

    def _start_clock(self):
        if self.armed and self._active_since is None:
            self._active_since = time.monotonic()

    def _stop_clock(self):
        if self._active_since is not None:
            self._active_seconds += time.monotonic() - self._active_since
            self._active_since = None

    def snapshot(self):
        with self.control:
            state = ("等待松开快捷键" if self._resume_pending else "已暂停") if self._paused else self._state
            remaining = max(0, self._total - self._sent)
            elapsed = self._active_seconds
            if self._active_since is not None:
                elapsed += time.monotonic() - self._active_since
            eta = ""
            if remaining and elapsed >= ETA_MIN_SECONDS and self._sent >= ETA_MIN_CHARACTERS:
                eta = format_eta(remaining * elapsed / self._sent)
            return {"state": state, "label": self.label, "reason": self._reason, "profile_name": self.profile_name,
                    "sent": self._sent, "total": self._total, "line": self._line, "lines": self._lines,
                    "remaining": remaining, "eta": eta,
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
            self._stop_clock()
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
                        self._start_clock()
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
        self.nap(max(MIN_KEY_DELAY_MS, self.cfg["keyDelay"]))

    def read_clipboard(self):
        for _ in range(50):
            self.check()
            if self.win.OpenClipboard(None):
                break
            self.nap(10)
        else:
            raise RuntimeError("无法打开剪贴板，请稍后再试")
        files, text = None, ""
        try:
            if self.win.IsClipboardFormatAvailable(CF_HDROP):
                files = self._read_clipboard_files()
            elif self.win.IsClipboardFormatAvailable(CF_UNICODETEXT):
                handle = self.win.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    raise C.WinError(C.get_last_error())
                pointer = self.win.GlobalLock(handle)
                if not pointer:
                    raise C.WinError(C.get_last_error())
                try:
                    size = self.win.GlobalSize(handle)
                    if size >= 2:
                        raw = C.string_at(pointer, size - size % 2)
                        text = raw.decode("utf-16-le", errors="surrogatepass").split("\0", 1)[0]
                finally:
                    self.win.GlobalUnlock(handle)
        finally:
            self.win.CloseClipboard()
        return self._read_files(files) if files is not None else text

    def _read_clipboard_files(self):
        handle = self.win.GetClipboardData(CF_HDROP)
        if not handle:
            raise C.WinError(C.get_last_error())
        count = self.win.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        paths = []
        for index in range(count):
            length = self.win.DragQueryFileW(handle, index, None, 0)
            buffer = C.create_unicode_buffer(length + 1)
            copied = self.win.DragQueryFileW(handle, index, buffer, len(buffer))
            if copied != length:
                raise RuntimeError("无法读取剪贴板中的文件路径")
            paths.append(Path(buffer.value))
        return paths

    def _read_files(self, paths):
        if not paths:
            return ""
        total = 0
        for path in paths:
            if not path.is_file():
                raise RuntimeError(f"剪贴板选中的项目不是普通文件：{path}")
            try:
                total += path.stat().st_size
            except OSError as exc:
                raise RuntimeError(f"无法读取文件：{path}（{exc}）") from exc
        # Typing runs at tens of characters per second, so a mistakenly
        # copied large file would otherwise occupy the target for hours.
        limit = self.options["max_file_kib"] * 1024
        if total > limit:
            raise RuntimeError(f"复制的文件共 {total / 1024:,.0f} KiB，超过上限 {limit // 1024:,} KiB；"
                               "可在设置中调高「复制文件大小上限」")
        files = []
        for path in paths:
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise RuntimeError(f"无法读取文件：{path}（{exc}）") from exc
            is_image = path.suffix.lower() in IMAGE_SUFFIXES
            files.append((path.name, data, None if is_image else decode_text_file(data)))
        if len(files) == 1 and files[0][2] is not None:
            # A single text file is typed as-is, ready to save on the target.
            return files[0][2]
        blocks = []
        for name, data, text in files:
            # Binary non-image files cannot be typed as text; a text body that
            # contains our markers would break splitting, so Base64 is kept.
            if text is None or "<<<CLIPBOARD_TYPER_" in text:
                blocks.append(base64_block(name, data))
            else:
                blocks.append(f"{TEXT_BEGIN_PREFIX}{name}{FILE_BEGIN_SUFFIX}\n{text}\n{FILE_END}")
        return "\n".join(blocks)

    def run(self):
        result, kind, detail = "", "notice", ""
        precise_timing = False
        try:
            precise_timing = self.win.begin_precise_timing()
            if self.physical.modifiers_down() or self.physical.down(self.trigger):
                with self.control:
                    self._state = "等待松开快捷键"
                self.publish("准备输入：请松开快捷键及 Ctrl / Alt / Shift / Win")
            while self.physical.modifiers_down() or self.physical.down(self.trigger):
                self.nap(10)
            with self.control:
                self._state = "准备输入"
            self.nap(self.options["start_delay_ms"])
            text = self.read_clipboard()
            if not text:
                result = "剪贴板是空的，或不含文本"
                self._set_state("无文本")
                return
            with self.control:
                self._state = "正在输入"
                if not self._paused:
                    self.publish(self.label + "输入中；" + self.shortcut("pause_resume") + " 暂停 / Esc 中止")
            self.type_text(text)
            self.check_cancelled()
            self._set_state("已完成")
            result = self.label + "输入完成"
        except Cancelled as exc:
            self._set_state("已中止")
            result = "已中止：" + str(exc)
        except Exception as exc:
            self._set_state("输入失败")
            result, kind = "输入失败：" + str(exc), "error"
            detail = traceback.format_exc()
        finally:
            if precise_timing:
                self.win.end_precise_timing()
            with self.control:
                self._stop_clock()
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
            if not self._paused:
                self._start_clock()
        self.report_progress(force=True)
        for index, line in enumerate(text.split("\n")):
            if index:
                self.tap(VK_RETURN, source_count=1, newline=True)
                if self.options["clear_auto_indent"]:
                    self.send([key_event(VK_SHIFT)] + key_pair(VK_HOME)
                              + [key_event(VK_SHIFT, flags=KEYUP)])
                    self.nap(max(MIN_KEY_DELAY_MS, cfg["keyDelay"]))
                    self.tap(VK_DELETE)
                self.nap(cfg["linePause"])
            for block_number, pos in enumerate(range(0, len(line), cfg["chunk"]), 1):
                block = line[pos:pos + cfg["chunk"]]
                for char in block:
                    # ASCII also uses VK_PACKET. Pace every source character,
                    # keeping both UTF-16 surrogate units in the same send.
                    self.send(text_events(char), source_count=1)
                    self.nap(max(MIN_KEY_DELAY_MS, cfg["keyDelay"]))
                self.nap(cfg["pause"])
                if cfg["breatherEvery"] and block_number % cfg["breatherEvery"] == 0:
                    self.nap(cfg["breatherPause"])
