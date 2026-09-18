"""Clipboard typing jobs, progress and pause/resume behavior."""
import copy
import ctypes as C
import threading
import time
import traceback
from clipboard_typer.core.config import DEFAULT_SETTINGS, parse_hotkey
from clipboard_typer.core.events import UiEvent
from clipboard_typer.platforms.windows import KEYUP, VK_DELETE, VK_HOME, VK_RETURN, VK_SHIFT, key_event, key_pair, text_events
from clipboard_typer.services.hotkeys import PhysicalKeys


# Leave time for the target to translate VK_PACKET before submitting the next
# character. A whole Unicode block can repeat/drop characters in some clients.
MIN_KEY_DELAY_MS = 10


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
        self.nap(max(MIN_KEY_DELAY_MS, self.cfg["keyDelay"]))

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
