"""Tk settings editor; all Tk objects stay on their own UI thread."""
import copy
import gc
import queue
import threading
import traceback
from clipboard_typer.platforms.windows import HANDLE, UINT
from clipboard_typer.ui.status_card import bind


PROFILE_FIELDS = [
    ("keyDelay", "每字延迟", "ms；0 / -1 为整块发送", -1, 60000),
    ("chunk", "每块字符数", "越大越快，暂停粒度越粗", 1, 256),
    ("pause", "块间停顿", "ms；掉字时可适当增大", 0, 60000),
    ("breatherEvery", "长停顿间隔", "块；0 为关闭", 0, 1000000),
    ("breatherPause", "长停顿时长", "ms", 0, 60000),
    ("linePause", "换行停顿", "ms", 0, 60000),
]
HOTKEY_FIELDS = [("slow", "慢速输入"), ("fast", "快速输入"),
                 ("pause_resume", "暂停 / 继续"), ("stop", "中止输入"), ("quit", "退出程序")]
BOOL_FIELDS = [
    ("show_popup", "显示状态卡片", "启动后显示右下角的小卡片，可从右键菜单临时隐藏。"),
    ("show_progress", "显示输入进度", "显示字符数量、百分比、行号和剩余量。"),
    ("stop_on_focus_loss", "保护原输入窗口", "关闭不会变成后台输入；内容可能被发往新焦点窗口。"),
    ("pause_on_focus_loss", "切换窗口时暂停", "关闭后，开启窗口保护时将直接中止任务。"),
    ("pause_on_modifiers", "手动按修饰键时暂停", "Ctrl / Alt / Shift / Win；松开后手动继续。"),
    ("clear_auto_indent", "换行后清除自动缩进", "发送 Shift+Home 和 Delete；不需要时可关闭。"),
]
NUMERIC_FIELDS = [("start_delay_ms", "启动等待", 0, 60000),
                  ("progress_interval_ms", "进度更新间隔", 100, 5000),
                  ("notice_duration_ms", "普通提示时长", 1000, 60000)]


class ConfigService:
    """Thread-safe facade. Only plain data crosses threads; Tk objects do not."""
    QUEUE_INTERVAL_MS = 50

    def __init__(self, win, notify, defaults, validator):
        self.win, self.notify, self.defaults, self.validator = win, notify, defaults, validator
        self.queue, self.lock = queue.SimpleQueue(), threading.Lock()
        self.thread = None
        self.closing = False

    def post(self, kind, data=None):
        with self.lock:
            if self.closing and kind != "quit":
                return
            self.queue.put((kind, data))

    def open(self, settings, path):
        self.post("open", (copy.deepcopy(settings), str(path)))
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.run, name="settings-ui", daemon=True)
                self.thread.start()

    def close(self):
        with self.lock:
            self.closing = True
        self.post("quit")
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)

    def run(self):
        root, editor = None, None
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            ancestor = bind(self.win.user, "GetAncestor", HANDLE, HANDLE, UINT)
            editor = SettingsEditor(root, self.defaults, self.validator, self.notify,
                                    window_handle=lambda: ancestor(root.winfo_id(), 2))
            def drain():
                # Even on this thread, calling Tk from a ctypes WNDPROC can
                # invalidate _tkinter's saved thread state inside mainloop.
                # Schedule exclusively from Tk callbacks, never native ones.
                # Schedule first so a recoverable UI error cannot stop delivery.
                root.after(self.QUEUE_INTERVAL_MS, drain)
                while True:
                    try:
                        kind, data = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "open":
                        editor.open(*data)
                    elif kind == "result":
                        editor.save_result(data)
                    elif kind == "record":
                        editor.record_result(data)
                    elif kind == "quit":
                        root.quit()
                        return
            root.after(0, drain)
            root.mainloop()
        except Exception as exc:
            self.notify("gui_failed", "无法打开设置窗口：" + str(exc), traceback.format_exc())
        finally:
            if root:
                try:
                    root.destroy()
                except Exception:
                    pass
            # Dispose Tcl-owned variables on their creator thread as well.
            editor, root = None, None
            gc.collect()


class SettingsEditor:
    BG, INK, MUTED, BLUE = "#F5F7FB", "#162238", "#778399", "#4169E1"

    def __init__(self, root, defaults, validator, notify, window_handle=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root, self.defaults, self.validator, self.notify = root, defaults, validator, notify
        self.variables, self.inputs, self.integer_labels = {}, {}, {}
        self.settings, self.path, self.saving, self.visible = None, "", False, False
        self.window_handle = window_handle or root.winfo_id
        self.recording, self.record_token, self.record_timer = None, 0, None
        self.record_buttons = {}
        root.title("ClipboardTyper · 设置")
        root.configure(bg=self.BG)
        scale = max(1.0, root.winfo_fpixels("1i") / 96)
        screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
        width = min(round(780 * scale), max(620, screen_width - 80))
        height = min(round(700 * scale), max(480, screen_height - 100))
        root.geometry(f"{width}x{height}+{max(0, (screen_width-width)//2)}+{max(0, (screen_height-height)//2)}")
        root.minsize(min(width, round(700 * scale)), min(height, round(600 * scale)))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.callback_error
        # Microsoft YaHei UI is supplied by Windows; Tk substitutes on other platforms.
        self.font = "Microsoft YaHei UI"
        root.option_add("*Font", (self.font, 10))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background=self.BG)
        style.configure("White.TFrame", background="#FFFFFF")
        style.configure("TLabel", background=self.BG, foreground=self.INK)
        style.configure("TNotebook", background=self.BG, borderwidth=0, tabmargins=(0, 0, 0, 12))
        style.configure("TNotebook.Tab", padding=(22, 11), background="#EAF0F8", foreground=self.MUTED, borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", "#FFFFFF")], foreground=[("selected", self.BLUE)])
        style.configure("TEntry", padding=7, fieldbackground="#FFFFFF", bordercolor="#DCE3EF", lightcolor="#DCE3EF", darkcolor="#DCE3EF")
        style.configure("TSpinbox", padding=6, fieldbackground="#FFFFFF", bordercolor="#DCE3EF")
        style.configure("TCheckbutton", background="#FFFFFF", foreground=self.INK, padding=(0, 3))
        style.map("TCheckbutton", background=[("active", "#FFFFFF")])
        style.configure("TButton", padding=(15, 9), background="#E9EEF7", foreground=self.INK, borderwidth=0)
        style.map("TButton", background=[("active", "#DDE6F4")])
        style.configure("Primary.TButton", background=self.BLUE, foreground="#FFFFFF")
        style.map("Primary.TButton", background=[("disabled", "#ADBCE8"), ("active", "#3458C5")], foreground=[("disabled", "#FFFFFF")])
        header = tk.Frame(root, bg=self.BG)
        header.pack(fill="x", padx=30, pady=(24, 18))
        logo = tk.Label(header, text=" C ", bg=self.BLUE, fg="#FFFFFF", font=(self.font, 19, "bold"), padx=7, pady=4)
        logo.pack(side="left", padx=(0, 14))
        title = tk.Frame(header, bg=self.BG)
        title.pack(side="left")
        tk.Label(title, text="ClipboardTyper", bg=self.BG, fg=self.INK, font=(self.font, 20, "bold")).pack(anchor="w")
        tk.Label(title, text="按你的节奏输入，每个选项都可以在这里调整。", bg=self.BG, fg=self.MUTED, font=(self.font, 10)).pack(anchor="w", pady=(3, 0))
        # Bottom controls are packed first, so they stay visible when resized.
        footer = tk.Frame(root, bg=self.BG)
        footer.pack(side="bottom", fill="x", padx=30, pady=(10, 20))
        self.status = tk.Label(footer, text="", bg=self.BG, fg=self.MUTED, anchor="w", justify="left", wraplength=width-80)
        self.status.pack(fill="x", pady=(0, 10))
        self.location = tk.Label(footer, text="", bg=self.BG, fg=self.MUTED, font=(self.font, 9), anchor="w", wraplength=width-80, justify="left")
        self.location.pack(fill="x", pady=(0, 10))
        buttons = tk.Frame(footer, bg=self.BG)
        buttons.pack(fill="x")
        self.reset_button = ttk.Button(buttons, text="恢复默认值", command=self.reset)
        self.reset_button.pack(side="left")
        self.save_button = ttk.Button(buttons, text="保存并应用", style="Primary.TButton", command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(buttons, text="关闭", command=self.close).pack(side="right", padx=(0, 10))
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=30)
        speed = self.page("通用速度")
        remote = self.page("远程桌面")
        shortcuts = self.page("快捷键")
        options = self.page("显示与保护")
        self.build_speed(speed)
        self.build_remote(remote)
        self.build_hotkeys(shortcuts)
        self.build_options(options)
        root.bind("<Control-s>", lambda event: self.save())
        root.bind("<FocusOut>", lambda event: root.after_idle(self.record_focus_check), add="+")

    def page(self, label):
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.tabs, style="White.TFrame")
        self.tabs.add(outer, text=label)
        canvas = tk.Canvas(outer, bg="#FFFFFF", highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        bar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        canvas.configure(yscrollcommand=bar.set)
        body = tk.Frame(canvas, bg="#FFFFFF", padx=20, pady=16)
        item = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        # Scrolling belongs to the visible page, not a process-wide bind_all.
        self.root.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units")
                       if self.tabs.select() == str(outer) else None, add="+")
        return body

    def label(self, parent, text, muted=False, **kwargs):
        return self.tk.Label(parent, text=text, bg="#FFFFFF", fg=self.MUTED if muted else self.INK,
                             font=(self.font, 9 if muted else 10), **kwargs)

    def spin(self, parent, path, label, lo, hi, width=10):
        variable = self.tk.StringVar(master=self.root)
        widget = self.ttk.Spinbox(parent, from_=lo, to=hi, textvariable=variable, width=width)
        self.variables[path], self.inputs[path] = variable, widget
        self.integer_labels[path] = label
        return widget

    def build_speed(self, page, prefix=("profiles",)):
        self.label(page, "快慢两套参数，单位为毫秒；保存后从下一次输入任务生效。", muted=True).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 18))
        self.label(page, "参数").grid(row=1, column=0, sticky="w", pady=(0, 12))
        self.label(page, "慢速 · 稳定优先").grid(row=1, column=1, sticky="w", padx=(14, 0), pady=(0, 12))
        self.label(page, "快速 · 整块发送").grid(row=1, column=2, sticky="w", padx=(14, 0), pady=(0, 12))
        for row, (key, label, hint, lo, hi) in enumerate(PROFILE_FIELDS, 2):
            cell = self.tk.Frame(page, bg="#FFFFFF")
            cell.grid(row=row, column=0, sticky="w", pady=7)
            self.label(cell, label).pack(anchor="w")
            self.label(cell, hint, muted=True).pack(anchor="w", pady=(2, 0))
            for column, mode in enumerate(("slow", "fast"), 1):
                self.spin(page, prefix + (mode, key), label, lo, hi).grid(row=row, column=column, sticky="ew", padx=(14, 0), pady=7)
        for column in (1, 2):
            page.grid_columnconfigure(column, weight=1)

    def build_remote(self, page):
        self.label(page, "只为远程桌面客户端使用独立速度；其他程序使用通用速度。",
                   muted=True, wraplength=580, justify="left").pack(anchor="w", pady=(0, 8))
        path = ("remote_desktop", "enabled")
        value = self.tk.BooleanVar(master=self.root)
        checkbox = self.ttk.Checkbutton(page, text="启用远程桌面专用速度", variable=value)
        checkbox.pack(anchor="w", pady=(0, 10))
        self.variables[path], self.inputs[path] = value, checkbox
        self.label(page, "客户端进程名（多个用逗号分隔）").pack(anchor="w")
        path = ("remote_desktop", "executables")
        value = self.tk.StringVar(master=self.root)
        entry = self.ttk.Entry(page, textvariable=value)
        entry.pack(fill="x", pady=(6, 6))
        self.variables[path], self.inputs[path] = value, entry
        self.label(page, "默认 mstsc.exe / msrdc.exe。其他客户端请按实际进程名填写。\n浏览器中的远程桌面不会自动识别；不建议把整个浏览器加入此列表。",
                   muted=True, wraplength=580, justify="left").pack(anchor="w", pady=(0, 15))
        path = ("remote_desktop", "citrix_enabled")
        value = self.tk.BooleanVar(master=self.root)
        checkbox = self.ttk.Checkbutton(page, text="Citrix Workspace 也使用此页速度", variable=value)
        checkbox.pack(anchor="w", pady=(0, 8))
        self.variables[path], self.inputs[path] = value, checkbox
        path = ("remote_desktop", "citrix_executables")
        value = self.tk.StringVar(master=self.root)
        entry = self.ttk.Entry(page, textvariable=value)
        entry.pack(fill="x", pady=(0, 6))
        self.variables[path], self.inputs[path] = value, entry
        self.label(page, "Citrix 会话进程（逗号分隔），支持 ICA 引擎及 Desktop Viewer。\n仅本地 Windows 客户端；浏览器 HTML5 会话不会自动识别。",
                   muted=True, wraplength=580, justify="left").pack(anchor="w", pady=(0, 15))
        grid = self.tk.Frame(page, bg="#FFFFFF")
        grid.pack(fill="x")
        self.build_speed(grid, ("remote_desktop", "profiles"))
        self.label(page, "运行本程序的电脑将按客户端进程匹配。切换焦点仍会暂停，\n这些参数不提供后台输入，也不能确认远端是否接收了每个字符。",
                   muted=True, wraplength=580, justify="left").pack(anchor="w", pady=(16, 0))

    def build_hotkeys(self, page):
        self.label(page, "输入组合，或点击「录制」后按组合键；Esc 取消录制。", muted=True).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 18))
        for row, (key, label) in enumerate(HOTKEY_FIELDS, 1):
            self.label(page, label).grid(row=row, column=0, sticky="w", padx=(0, 26), pady=10)
            path = ("hotkeys", key)
            variable = self.tk.StringVar(master=self.root)
            widget = self.ttk.Entry(page, textvariable=variable, width=28)
            widget.grid(row=row, column=1, sticky="ew", pady=10)
            self.variables[path], self.inputs[path] = variable, widget
            button = self.ttk.Button(page, text="录制", command=lambda action=key: self.begin_record(action))
            button.grid(row=row, column=2, padx=(12, 0), pady=10)
            self.record_buttons[key] = button
        page.grid_columnconfigure(1, weight=1)
        self.label(page, "字母 / 数字必须配 Ctrl、Alt 或 Win；仅 Shift 不接受。\nEsc 只在原目标窗口中止任务。F12 为系统保留键。\n录制不会执行已绑定的动作；保存时检查重复和占用。", muted=True, justify="left").grid(row=7, column=0, columnspan=3, sticky="w", pady=(22, 6))

    def begin_record(self, action):
        if self.saving:
            return
        if self.recording:
            self.cancel_record()
            return
        hwnd = self.window_handle()
        if not hwnd:
            raise RuntimeError("无法识别设置窗口，请重新打开设置后录制")
        self.record_token += 1
        self.recording = action
        self.set_editable(False)
        self.record_buttons[action].state(["!disabled"])
        self.record_buttons[action].configure(text="取消")
        self.status.configure(text="正在准备录制…", fg=self.BLUE)
        self.notify("record_start", data={"action": action, "token": self.record_token,
                                          "hwnd": hwnd})
        self.record_timer = self.root.after(15000, self.cancel_record)

    def end_record_ui(self):
        if self.record_timer is not None:
            self.root.after_cancel(self.record_timer)
            self.record_timer = None
        if self.recording:
            self.record_buttons[self.recording].configure(text="录制")
        self.recording = None
        self.set_editable(True)

    def cancel_record(self):
        if self.recording:
            self.notify("record_cancel", data=self.record_token)
            self.end_record_ui()
            # Invalidate a result already queued by the hook before cancellation.
            self.record_token += 1
            self.status.configure(text="录制已取消；原快捷键未变。", fg=self.MUTED)

    def record_focus_check(self):
        if self.recording and self.root.focus_displayof() is None:
            self.cancel_record()

    def record_result(self, result):
        if not self.recording or result["token"] != self.record_token:
            return
        if result.get("label"):
            self.variables[("hotkeys", result["action"])].set(result["label"])
        if result["done"]:
            self.end_record_ui()
        self.status.configure(text=result["text"], fg=self.BLUE)

    def build_options(self, page):
        for key, label, description in BOOL_FIELDS:
            path = ("options", key)
            variable = self.tk.BooleanVar(master=self.root)
            checkbox = self.ttk.Checkbutton(page, text=label, variable=variable)
            checkbox.pack(anchor="w", pady=(5, 0))
            self.label(page, description, muted=True, wraplength=590, justify="left").pack(anchor="w", padx=(23, 0), pady=(0, 8))
            self.variables[path], self.inputs[path] = variable, checkbox
        numbers = self.tk.Frame(page, bg="#FFFFFF")
        numbers.pack(fill="x", pady=(10, 0))
        for row, (key, label, lo, hi) in enumerate(NUMERIC_FIELDS):
            self.label(numbers, label + "（ms）").grid(row=row, column=0, sticky="w", padx=(0, 20), pady=6)
            self.spin(numbers, ("options", key), label, lo, hi, 14).grid(row=row, column=1, sticky="w", pady=6)

    def populate(self, settings):
        for path, variable in self.variables.items():
            value = settings
            for part in path:
                value = value[part]
            if path in (("remote_desktop", "executables"), ("remote_desktop", "citrix_executables")):
                value = ", ".join(value)
            variable.set(value)

    def open(self, settings, path):
        if not self.visible:
            self.settings, self.path = copy.deepcopy(settings), path
            self.populate(settings)
            self.location.configure(text="配置文件：" + path)
            self.status.configure(text="保存后立即应用快捷键；速度与输入保护从下一次任务生效。", fg=self.MUTED)
        self.visible = True
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def reset(self):
        if not self.saving and not self.recording:
            self.populate(self.defaults)
            self.status.configure(text="已填入默认值；点击「保存并应用」后生效。", fg=self.MUTED)

    def collect(self):
        candidate = copy.deepcopy(self.settings)
        for path, variable in self.variables.items():
            value = variable.get()
            if path in self.integer_labels:
                try:
                    value = int(value.strip())
                except (ValueError, AttributeError):
                    self.inputs[path].focus_set()
                    raise ValueError(self.integer_labels[path] + "必须填写整数") from None
            elif path[0] == "hotkeys":
                value = value.strip()
            elif path in (("remote_desktop", "executables"), ("remote_desktop", "citrix_executables")):
                value = [name.strip() for name in value.replace("，", ",").split(",") if name.strip()]
            target = candidate
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
        return self.validator(candidate)

    def save(self):
        if self.saving or self.recording:
            return
        try:
            candidate = self.collect()
        except Exception as exc:
            self.status.configure(text="未保存：" + str(exc), fg="#C14450")
            return
        self.saving = True
        self.set_editable(False)
        self.status.configure(text="正在保存并检查快捷键…", fg=self.MUTED)
        self.notify("gui_save", data=candidate)

    def set_editable(self, enabled):
        state = "!disabled" if enabled else "disabled"
        self.save_button.state([state])
        self.reset_button.state([state])
        for widget in self.inputs.values():
            widget.state([state])
        for button in self.record_buttons.values():
            button.state([state])

    def save_result(self, result):
        self.saving = False
        self.set_editable(True)
        if result["ok"]:
            self.settings = copy.deepcopy(result["settings"])
            self.populate(self.settings)
            self.status.configure(text="已保存并应用。回到原输入位置后，按暂停 / 继续快捷键恢复任务。", fg="#168569")
        else:
            self.status.configure(text="未保存：" + result["error"], fg="#C14450")

    def close(self):
        if self.saving:
            self.status.configure(text="正在保存，请稍候再关闭。", fg=self.MUTED)
            return
        self.cancel_record()
        self.visible = False
        self.root.withdraw()
        self.notify("gui_closed")

    def callback_error(self, kind, error, tb):
        self.cancel_record()
        self.status.configure(text="窗口操作失败：" + str(error), fg="#C14450")
        self.notify("gui_error", str(error), "".join(traceback.format_exception(kind, error, tb)))
