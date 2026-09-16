"""Application lifecycle and coordination of services and views."""
from pathlib import Path
import copy
import ctypes as C
import math
import subprocess
import threading
import time
import traceback
from clipboard_typer.core.config import ConfigError, DEFAULT_SETTINGS, parse_hotkey, read_settings, save_settings_atomic, validate_settings
from clipboard_typer.core.events import UiEvent
from clipboard_typer.platforms.windows import EventBus, HANDLE, INFINITE, KBDLLHOOKSTRUCT, MODIFIER_KEYS, MSG, VK_ESCAPE, WAIT_FAILED, WM_HOTKEY, WM_QUIT
from clipboard_typer.services.hotkeys import HotkeyManager, PhysicalKeys
from clipboard_typer.services.profiles import select_speed_profile
from clipboard_typer.services.typing_service import TypingJob
from clipboard_typer.ui.settings import ConfigService
from clipboard_typer.ui.status_card import StatusCard
from clipboard_typer.ui.tray import TrayIcon


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
