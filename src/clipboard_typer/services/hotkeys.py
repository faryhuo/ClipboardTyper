"""Transactional hotkey registration and physical key state."""
import threading
from clipboard_typer.core.config import ConfigError, parse_hotkey
from clipboard_typer.platforms.windows import MODIFIER_KEYS, MOD_NOREPEAT, SIDE_MODIFIER_KEYS, VK_CONTROL, VK_MENU, VK_SHIFT


class HotkeyManager:
    """先注册新增组合，全部成功后才释放旧组合；失败保留原绑定。"""
    def __init__(self, win, hwnd):
        self.win, self.hwnd = win, hwnd
        self.bindings, self.actions, self.next_id = {}, {}, 1
        self.pending = None

    def stage(self, values):
        if self.pending is not None:
            raise ConfigError("正在保存快捷键，请稍候")
        desired = [parse_hotkey(action, value) for action, value in values.items()]
        if len({key.combo for key in desired}) != len(desired):
            raise ConfigError("快捷键重复")
        staged = {}
        try:
            for key in desired:
                if key.combo in self.bindings:
                    continue
                hotkey_id = self.next_id
                self.next_id += 1
                if hotkey_id > 0xBFFF:
                    raise ConfigError("快捷键重载次数过多，请重新启动程序")
                if not self.win.RegisterHotKey(self.hwnd, hotkey_id, key.modifiers | MOD_NOREPEAT, key.key):
                    raise ConfigError(f"无法注册 {key.label}，可能被其他程序占用；原快捷键保持有效")
                staged[key.combo] = hotkey_id
        except Exception:
            for hotkey_id in staged.values():
                self.win.UnregisterHotKey(self.hwnd, hotkey_id)
            raise
        self.pending = desired, staged

    def commit(self):
        desired, staged = self.pending
        combined = dict(self.bindings)
        combined.update(staged)
        bindings = {key.combo: combined[key.combo] for key in desired}
        actions = {bindings[key.combo]: key.action for key in desired}
        retired = [hotkey_id for combo, hotkey_id in self.bindings.items() if combo not in bindings]
        self.bindings, self.actions = bindings, actions
        self.pending = None
        for hotkey_id in retired:
            self.win.UnregisterHotKey(self.hwnd, hotkey_id)

    def rollback(self):
        if self.pending is not None:
            for hotkey_id in self.pending[1].values():
                self.win.UnregisterHotKey(self.hwnd, hotkey_id)
            self.pending = None

    def apply(self, values):
        self.stage(values)
        self.commit()

    def close(self):
        self.rollback()
        for hotkey_id in self.bindings.values():
            self.win.UnregisterHotKey(self.hwnd, hotkey_id)
        self.bindings.clear()
        self.actions.clear()


class PhysicalKeys:
    """只记物理事件；忽略本程序和其他程序注入的按键。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.pressed = set()

    def seed(self, win, extra_keys=()):
        with self.lock:
            for vk in set(SIDE_MODIFIER_KEYS) | set(extra_keys):
                if win.key_down(vk):
                    self.pressed.add(vk)

    def observe(self, vk, down, injected=False):
        if injected:
            return False
        with self.lock:
            if down:
                self.pressed.add(vk)
            else:
                self.pressed.discard(vk)
                generic = {0xA0: VK_SHIFT, 0xA1: VK_SHIFT, 0xA2: VK_CONTROL,
                           0xA3: VK_CONTROL, 0xA4: VK_MENU, 0xA5: VK_MENU}.get(vk)
                if generic:
                    self.pressed.discard(generic)
        return True

    def down(self, vk):
        with self.lock:
            return vk in self.pressed

    def modifiers_down(self):
        with self.lock:
            return bool(self.pressed & MODIFIER_KEYS)
