"""Small Win32 adapter; no hooks, injection, or game process access."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import queue
import threading
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.GetAncestor.argtypes = [w.HWND, w.UINT]
user32.GetAncestor.restype = w.HWND
user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
user32.SetWindowPos.restype = w.BOOL
user32.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
user32.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
user32.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
user32.PeekMessageW.restype = w.BOOL
user32.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
user32.WindowFromPoint.argtypes = [w.POINT]
user32.WindowFromPoint.restype = w.HWND
user32.IsWindowVisible.argtypes = [w.HWND]
user32.IsWindowVisible.restype = w.BOOL
user32.keybd_event.argtypes = [w.BYTE, w.BYTE, w.DWORD, ctypes.c_size_t]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
kernel32.CreateMutexW.restype = w.HANDLE
kernel32.CloseHandle.argtypes = [w.HANDLE]

GWL_EXSTYLE = -20
TRANSPARENT = 0x00000020
LAYERED = 0x00080000
NOACTIVATE = 0x08000000
TOPMOST = 0x00000008
WM_HOTKEY = 0x0312


def enable_dpi() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


def root_handle(root) -> int:
    root.update_idletasks()
    return int(user32.GetAncestor(root.winfo_id(), 2))


def copy_file_to_clipboard(path, hwnd: int) -> None:
    """Copy an existing file as CF_HDROP, so WeChat receives an attachment."""
    from .settings import f_path
    import struct

    path = f_path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = struct.pack("<IiiII", 20, 0, 0, 0, 1) + (str(path) + "\0\0").encode("utf-16-le")
    kernel32.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = w.HGLOBAL
    kernel32.GlobalLock.argtypes = [w.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [w.HGLOBAL]
    kernel32.GlobalFree.argtypes = [w.HGLOBAL]
    kernel32.GlobalFree.restype = w.HGLOBAL
    user32.OpenClipboard.argtypes = [w.HWND]
    user32.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
    user32.SetClipboardData.restype = w.HANDLE
    memory = kernel32.GlobalAlloc(0x0042, len(payload))
    if not memory:
        raise ctypes.WinError(ctypes.get_last_error())
    opened = False
    try:
        pointer = kernel32.GlobalLock(memory)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(pointer, payload, len(payload))
        finally:
            kernel32.GlobalUnlock(memory)
        for _ in range(5):
            if user32.OpenClipboard(hwnd):
                opened = True
                break
            time.sleep(0.02)
        if not opened:
            raise RuntimeError("剪贴板正被其他程序占用，文件已保留在 exports 文件夹。")
        if not user32.EmptyClipboard() or not user32.SetClipboardData(15, memory):
            raise ctypes.WinError(ctypes.get_last_error())
        memory = None  # Windows owns it after SetClipboardData succeeds.
    finally:
        if opened:
            user32.CloseClipboard()
        if memory:
            kernel32.GlobalFree(memory)


def set_click_through(hwnd: int, enabled: bool) -> None:
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if enabled:
        style |= TRANSPARENT | LAYERED | NOACTIVATE
    else:
        style &= ~(TRANSPARENT | NOACTIVATE)
    ctypes.set_last_error(0)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    if ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    user32.SetWindowPos(hwnd, w.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0020)


def parse_hotkey(text: str) -> tuple[int, int]:
    parts = text.upper().replace(" ", "").split("+")
    if len(parts) < 2:
        raise ValueError("快捷键需要 CTRL、ALT 或 SHIFT 修饰键。")
    modifiers = 0x4000  # MOD_NOREPEAT
    mapping = {"ALT": 1, "CTRL": 2, "SHIFT": 4}
    for part in parts[:-1]:
        if part not in mapping:
            raise ValueError("快捷键格式示例：CTRL+ALT+B")
        modifiers |= mapping[part]
    key = parts[-1]
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key)
    elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError("快捷键只支持字母、数字和 F1–F12。")
    return modifiers, vk


def exercise_hotkey(text: str) -> None:
    """Issue a registered shortcut for the explicitly requested UI self-test."""
    modifiers, key = parse_hotkey(text)
    keys = [vk for mask, vk in ((2, 0x11), (1, 0x12), (4, 0x10)) if modifiers & mask]
    try:
        for vk in keys:
            user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(key, 0, 0, 0)
        user32.keybd_event(key, 0, 2, 0)
    finally:
        for vk in reversed(keys):
            user32.keybd_event(vk, 0, 2, 0)


class Hotkeys:
    def __init__(self):
        self.registered = []
        self.commands = queue.Queue()
        self.events = queue.Queue()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="scout-hotkeys")
        self.thread.start()
        self.ready.wait(2)

    def _run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        message = w.MSG()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        self.ready.set()
        running = True
        while running:
            try:
                while True:
                    command = self.commands.get_nowait()
                    if command is None:
                        running = False
                        break
                    identifier, modifiers, vk, done, result = command
                    ok = bool(user32.RegisterHotKey(None, identifier, modifiers, vk))
                    if ok:
                        self.registered.append(identifier)
                    result.append(ok)
                    done.set()
            except queue.Empty:
                pass
            while user32.PeekMessageW(ctypes.byref(message), None, WM_HOTKEY, WM_HOTKEY, 1):
                self.events.put(int(message.wParam))
            time.sleep(0.015)
        for identifier in self.registered:
            user32.UnregisterHotKey(None, identifier)
        self.registered.clear()

    def register(self, identifier: int, text: str) -> bool:
        modifiers, vk = parse_hotkey(text)
        done = threading.Event()
        result = []
        self.commands.put((identifier, modifiers, vk, done, result))
        return bool(done.wait(2) and result[0])

    def poll(self) -> list[int]:
        result = []
        try:
            while True:
                result.append(self.events.get_nowait())
        except queue.Empty:
            pass
        return result

    def close(self):
        self.commands.put(None)
        self.thread.join(0.5)


class SingleInstance:
    def __init__(self):
        self.handle = kernel32.CreateMutexW(None, False, "Local\\BrokenArrowScout_Portable_06")
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.already_running = ctypes.get_last_error() == 183

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None
