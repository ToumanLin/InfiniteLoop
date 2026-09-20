#!/usr/bin/env python3
"""External Windows control CLI for Punishing: Gray Raven (PGR.exe).

Provides guarded, best-effort window enumeration, focus activation,
client-relative screenshot capture, KRSDK UI login automation, and
guarded client-relative mouse/keyboard input.

Win32 Limitation Notice:
Win32 SendInput injects into the global OS input queue, and screen capture
samples desktop pixels within the target client rectangle. While target
identity, client dimensions, and foreground states are strictly verified
immediately before and after actions, Win32 does not provide atomic
isolation against transient foreground races or occluding topmost windows.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# -----------------------------------------------------------------------------
# Win32 Constants
# -----------------------------------------------------------------------------
GW_OWNER = 4
GW_CHILD = 5
GW_HWNDNEXT = 2
GWLP_HWNDPARENT = -8

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_RESTORE = 9

BM_CLICK = 0x00F5
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
BN_CLICKED = 0
IDOK = 1
IDCANCEL = 2

# KRSDK Exact Child Control IDs
ID_BTN_LOGIN = 10001
ID_BTN_REGISTER = 10002
ID_EDIT_USERNAME = 1001
ID_EDIT_PASSWORD = 1002
ID_BTN_SUBMIT = 1
ID_BTN_CANCEL = 2

# Screen and Input Metrics
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# Key mapping
VK_MAP = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "escape": 0x1B,
    "esc": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}
for c in range(ord("a"), ord("z") + 1):
    VK_MAP[chr(c)] = ord(chr(c).upper())
for c in range(ord("0"), ord("9") + 1):
    VK_MAP[chr(c)] = c
for number in range(1, 25):
    VK_MAP[f"f{number}"] = 0x70 + number - 1

# -----------------------------------------------------------------------------
# Win32 Ctypes Structures
# -----------------------------------------------------------------------------
class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUT_UNION),
    ]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


# -----------------------------------------------------------------------------
# Driver Abstraction
# -----------------------------------------------------------------------------
class Win32Driver:
    """Abstract interface for Win32 OS interactions."""

    def open_input_desktop(self) -> Any:
        raise NotImplementedError

    def close_desktop(self, handle: Any) -> None:
        raise NotImplementedError

    def set_thread_desktop(self, handle: Any) -> bool:
        raise NotImplementedError

    def enum_desktop_windows(self, h_desk: Any, callback: Any) -> bool:
        raise NotImplementedError

    def enum_windows(self, callback: Any) -> bool:
        raise NotImplementedError

    def get_window_thread_process_id(self, hwnd: int) -> Tuple[int, int]:
        """Returns (thread_id, pid)."""
        raise NotImplementedError

    def get_process_image_path(self, pid: int) -> str:
        raise NotImplementedError

    def get_class_name(self, hwnd: int) -> str:
        raise NotImplementedError

    def get_window_text(self, hwnd: int) -> str:
        raise NotImplementedError

    def get_window_owner(self, hwnd: int) -> int:
        raise NotImplementedError

    def is_window(self, hwnd: int) -> bool:
        raise NotImplementedError

    def is_window_visible(self, hwnd: int) -> bool:
        raise NotImplementedError

    def is_iconic(self, hwnd: int) -> bool:
        raise NotImplementedError

    def get_window_rect(self, hwnd: int) -> Tuple[int, int, int, int]:
        """Returns (left, top, right, bottom)."""
        raise NotImplementedError

    def get_client_rect(self, hwnd: int) -> Tuple[int, int, int, int]:
        """Returns (0, 0, width, height)."""
        raise NotImplementedError

    def client_to_screen(self, hwnd: int, x: int, y: int) -> Tuple[int, int]:
        raise NotImplementedError

    def get_foreground_window(self) -> int:
        raise NotImplementedError

    def set_foreground_window(self, hwnd: int) -> bool:
        raise NotImplementedError

    def switch_to_this_window(self, hwnd: int, alt_tab: bool) -> None:
        raise NotImplementedError

    def show_window(self, hwnd: int, cmd: int) -> bool:
        raise NotImplementedError

    def bring_window_to_top(self, hwnd: int) -> bool:
        raise NotImplementedError

    def set_active_window(self, hwnd: int) -> int:
        raise NotImplementedError

    def attach_thread_input(self, tid1: int, tid2: int, attach: bool) -> bool:
        raise NotImplementedError

    def allow_set_foreground_window(self, pid: int) -> bool:
        raise NotImplementedError

    def get_current_thread_id(self) -> int:
        raise NotImplementedError

    def get_dlg_item(self, h_dialog: int, item_id: int) -> int:
        raise NotImplementedError

    def send_message(self, hwnd: int, msg: int, wparam: int, lparam: Any) -> int:
        raise NotImplementedError

    def post_message(self, hwnd: int, msg: int, wparam: int, lparam: Any) -> bool:
        raise NotImplementedError

    def send_mouse_click(self, screen_x: int, screen_y: int) -> bool:
        raise NotImplementedError

    def send_key_press(self, vk_code: int) -> bool:
        raise NotImplementedError

    def grab_screen(self, bbox: Tuple[int, int, int, int]) -> Any:
        raise NotImplementedError


class LiveWin32Driver(Win32Driver):
    """Production Win32 driver backed by user32 and kernel32."""

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # Setup ctypes signatures
        self.user32.OpenInputDesktop.restype = wintypes.HANDLE
        self.user32.OpenInputDesktop.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]

        self.user32.CloseDesktop.restype = wintypes.BOOL
        self.user32.CloseDesktop.argtypes = [wintypes.HANDLE]

        self.user32.SetThreadDesktop.restype = wintypes.BOOL
        self.user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]

        self.user32.EnumDesktopWindows.restype = wintypes.BOOL
        self.user32.EnumDesktopWindows.argtypes = [
            wintypes.HANDLE,
            WNDENUMPROC,
            wintypes.LPARAM,
        ]

        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]

        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetForegroundWindow.argtypes = []

        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]

        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsWindow.argtypes = [wintypes.HWND]

        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]

        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]

        self.user32.GetWindow.restype = wintypes.HWND
        self.user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]

        self.user32.GetClassNameW.restype = ctypes.c_int
        self.user32.GetClassNameW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]

        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]

        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]

        self.user32.GetClientRect.restype = wintypes.BOOL
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]

        self.user32.ClientToScreen.restype = wintypes.BOOL
        self.user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]

        self.user32.GetDlgItem.restype = wintypes.HWND
        self.user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]

        self.user32.SendMessageW.restype = wintypes.LPARAM
        self.user32.SendMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]

        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]

        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]

        self.user32.SwitchToThisWindow.restype = None
        self.user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]

        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

        self.user32.BringWindowToTop.restype = wintypes.BOOL
        self.user32.BringWindowToTop.argtypes = [wintypes.HWND]

        self.user32.SetActiveWindow.restype = wintypes.HWND
        self.user32.SetActiveWindow.argtypes = [wintypes.HWND]

        self.user32.AttachThreadInput.restype = wintypes.BOOL
        self.user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]

        self.user32.AllowSetForegroundWindow.restype = wintypes.BOOL
        self.user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]

        self.user32.SendInput.restype = wintypes.UINT
        self.user32.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        ]

        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]

        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]

        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]

        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.kernel32.GetCurrentThreadId.argtypes = []

    def open_input_desktop(self) -> Any:
        # DESKTOP_READOBJECTS | DESKTOP_ENUMERATE. Enumeration does not require
        # switching the caller thread to the returned desktop.
        return self.user32.OpenInputDesktop(0, False, 0x0041)

    def close_desktop(self, handle: Any) -> None:
        if handle:
            self.user32.CloseDesktop(handle)

    def set_thread_desktop(self, handle: Any) -> bool:
        if handle:
            return bool(self.user32.SetThreadDesktop(handle))
        return False

    def enum_desktop_windows(self, h_desk: Any, callback: Any) -> bool:
        c_cb = WNDENUMPROC(callback)
        return bool(self.user32.EnumDesktopWindows(h_desk, c_cb, 0))

    def enum_windows(self, callback: Any) -> bool:
        c_cb = WNDENUMPROC(callback)
        return bool(self.user32.EnumWindows(c_cb, 0))

    def get_window_thread_process_id(self, hwnd: int) -> Tuple[int, int]:
        pid = wintypes.DWORD()
        tid = self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return (int(tid), int(pid.value))

    def get_process_image_path(self, pid: int) -> str:
        h = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            res = self.kernel32.QueryFullProcessImageNameW(
                h, 0, buf, ctypes.byref(size)
            )
            return buf.value if res else ""
        finally:
            self.kernel32.CloseHandle(h)

    def get_class_name(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def get_window_text(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(512)
        self.user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value

    def get_window_owner(self, hwnd: int) -> int:
        return int(self.user32.GetWindow(hwnd, GW_OWNER) or 0)

    def is_window(self, hwnd: int) -> bool:
        return bool(self.user32.IsWindow(hwnd))

    def is_window_visible(self, hwnd: int) -> bool:
        return bool(self.user32.IsWindowVisible(hwnd))

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self.user32.IsIconic(hwnd))

    def get_window_rect(self, hwnd: int) -> Tuple[int, int, int, int]:
        rc = RECT()
        self.user32.GetWindowRect(hwnd, ctypes.byref(rc))
        return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))

    def get_client_rect(self, hwnd: int) -> Tuple[int, int, int, int]:
        rc = RECT()
        self.user32.GetClientRect(hwnd, ctypes.byref(rc))
        return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))

    def client_to_screen(self, hwnd: int, x: int, y: int) -> Tuple[int, int]:
        pt = POINT(x, y)
        self.user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return (int(pt.x), int(pt.y))

    def get_foreground_window(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def set_foreground_window(self, hwnd: int) -> bool:
        return bool(self.user32.SetForegroundWindow(hwnd))

    def switch_to_this_window(self, hwnd: int, alt_tab: bool) -> None:
        self.user32.SwitchToThisWindow(hwnd, alt_tab)

    def show_window(self, hwnd: int, cmd: int) -> bool:
        return bool(self.user32.ShowWindow(hwnd, cmd))

    def bring_window_to_top(self, hwnd: int) -> bool:
        return bool(self.user32.BringWindowToTop(hwnd))

    def set_active_window(self, hwnd: int) -> int:
        return int(self.user32.SetActiveWindow(hwnd) or 0)

    def attach_thread_input(self, tid1: int, tid2: int, attach: bool) -> bool:
        return bool(self.user32.AttachThreadInput(tid1, tid2, attach))

    def allow_set_foreground_window(self, pid: int) -> bool:
        return bool(self.user32.AllowSetForegroundWindow(pid))

    def get_current_thread_id(self) -> int:
        return int(self.kernel32.GetCurrentThreadId())

    def get_dlg_item(self, h_dialog: int, item_id: int) -> int:
        return int(self.user32.GetDlgItem(h_dialog, item_id) or 0)

    def send_message(self, hwnd: int, msg: int, wparam: int, lparam: Any) -> int:
        if isinstance(lparam, str):
            text_buffer = ctypes.create_unicode_buffer(lparam)
            text_ptr = ctypes.cast(text_buffer, ctypes.c_void_p).value or 0
            return int(self.user32.SendMessageW(hwnd, msg, wparam, text_ptr))
        return int(self.user32.SendMessageW(hwnd, msg, wparam, lparam or 0))

    def post_message(self, hwnd: int, msg: int, wparam: int, lparam: Any) -> bool:
        return bool(self.user32.PostMessageW(hwnd, msg, wparam, lparam or 0))

    def send_mouse_click(self, screen_x: int, screen_y: int) -> bool:
        v_left = self.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        v_top = self.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        v_width = self.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        v_height = self.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        if v_width <= 1 or v_height <= 1:
            v_left = 0
            v_top = 0
            v_width = max(1, self.user32.GetSystemMetrics(SM_CXSCREEN))
            v_height = max(1, self.user32.GetSystemMetrics(SM_CYSCREEN))

        norm_x = int((screen_x - v_left) * 65535 / (v_width - 1))
        norm_y = int((screen_y - v_top) * 65535 / (v_height - 1))

        flags_base = (
            MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE
        )
        inputs = (INPUT * 3)()

        # Move
        inputs[0].type = INPUT_MOUSE
        inputs[0].u.mi.dx = norm_x
        inputs[0].u.mi.dy = norm_y
        inputs[0].u.mi.dwFlags = flags_base

        # Down
        inputs[1].type = INPUT_MOUSE
        inputs[1].u.mi.dx = norm_x
        inputs[1].u.mi.dy = norm_y
        inputs[1].u.mi.dwFlags = flags_base | MOUSEEVENTF_LEFTDOWN

        # Up
        inputs[2].type = INPUT_MOUSE
        inputs[2].u.mi.dx = norm_x
        inputs[2].u.mi.dy = norm_y
        inputs[2].u.mi.dwFlags = flags_base | MOUSEEVENTF_LEFTUP

        sent = self.user32.SendInput(3, inputs, ctypes.sizeof(INPUT))
        return sent == 3

    def send_key_press(self, vk_code: int) -> bool:
        inputs = (INPUT * 2)()

        # Key down
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].u.ki.wVk = vk_code

        # Key up
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].u.ki.wVk = vk_code
        inputs[1].u.ki.dwFlags = KEYEVENTF_KEYUP

        sent = self.user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
        return sent == 2

    def grab_screen(self, bbox: Tuple[int, int, int, int]) -> Any:
        try:
            from PIL import ImageGrab
        except ImportError:
            raise RuntimeError(
                "Pillow is not installed. Pillow is an optional dependency required for screenshots. "
                "Install it with 'pip install pillow'."
            )
        return ImageGrab.grab(bbox=bbox, all_screens=True)


class FakeWindow:
    """Mock window representation for unit tests."""

    def __init__(
        self,
        hwnd: int,
        pid: int,
        class_name: str,
        title: str = "",
        owner_hwnd: int = 0,
        is_visible: bool = True,
        is_iconic: bool = False,
        window_rect: Tuple[int, int, int, int] = (0, 0, 1536, 960),
        client_rect: Tuple[int, int, int, int] = (0, 0, 1536, 960),
        client_origin: Tuple[int, int] = (0, 0),
    ) -> None:
        self.hwnd = hwnd
        self.pid = pid
        self.class_name = class_name
        self.title = title
        self.owner_hwnd = owner_hwnd
        self.is_visible = is_visible
        self.is_iconic = is_iconic
        self.window_rect = window_rect
        self.client_rect = client_rect
        self.client_origin = client_origin
        self.children: Dict[int, int] = {}
        self.text: str = title


class FakeWin32Driver(Win32Driver):
    """Configurable mock driver for testing without interacting with live Windows GUI."""

    def __init__(self) -> None:
        self.processes: Dict[int, str] = {}
        self.windows: Dict[int, FakeWindow] = {}
        self.foreground_hwnd: int = 0
        self.current_thread_id: int = 1000
        self.threads: Dict[int, Tuple[int, int]] = {}  # hwnd -> (tid, pid)

        # Simulation behavior knobs
        self.foreground_race_during_screenshot: bool = False
        self.foreground_race_during_click: bool = False
        self.foreground_race_during_key: bool = False
        self.auth_simulation: str = "success"  # "success", "error", "timeout"
        self.error_message: str = "Invalid account or password"
        self.pillow_available: bool = True
        self.recorded_actions: List[Dict[str, Any]] = []
        self.submit_count: int = 0

    def open_input_desktop(self) -> Any:
        return 12345

    def close_desktop(self, handle: Any) -> None:
        pass

    def set_thread_desktop(self, handle: Any) -> bool:
        return True

    def enum_desktop_windows(self, h_desk: Any, callback: Any) -> bool:
        for hwnd in list(self.windows.keys()):
            if not callback(hwnd, 0):
                break
        return True

    def enum_windows(self, callback: Any) -> bool:
        for hwnd in list(self.windows.keys()):
            if not callback(hwnd, 0):
                break
        return True

    def get_window_thread_process_id(self, hwnd: int) -> Tuple[int, int]:
        win = self.windows.get(hwnd)
        if win:
            return (self.threads.get(hwnd, (1001, win.pid)))
        return (0, 0)

    def get_process_image_path(self, pid: int) -> str:
        return self.processes.get(pid, "")

    def get_class_name(self, hwnd: int) -> str:
        win = self.windows.get(hwnd)
        return win.class_name if win else ""

    def get_window_text(self, hwnd: int) -> str:
        win = self.windows.get(hwnd)
        return win.text if win else ""

    def get_window_owner(self, hwnd: int) -> int:
        win = self.windows.get(hwnd)
        return win.owner_hwnd if win else 0

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self.windows

    def is_window_visible(self, hwnd: int) -> bool:
        win = self.windows.get(hwnd)
        return win.is_visible if win else False

    def is_iconic(self, hwnd: int) -> bool:
        win = self.windows.get(hwnd)
        return win.is_iconic if win else False

    def get_window_rect(self, hwnd: int) -> Tuple[int, int, int, int]:
        win = self.windows.get(hwnd)
        return win.window_rect if win else (0, 0, 0, 0)

    def get_client_rect(self, hwnd: int) -> Tuple[int, int, int, int]:
        win = self.windows.get(hwnd)
        return win.client_rect if win else (0, 0, 0, 0)

    def client_to_screen(self, hwnd: int, x: int, y: int) -> Tuple[int, int]:
        win = self.windows.get(hwnd)
        if win:
            ox, oy = win.client_origin
            return (ox + x, oy + y)
        return (x, y)

    def get_foreground_window(self) -> int:
        return self.foreground_hwnd

    def set_foreground_window(self, hwnd: int) -> bool:
        if hwnd in self.windows:
            self.foreground_hwnd = hwnd
            return True
        return False

    def switch_to_this_window(self, hwnd: int, alt_tab: bool) -> None:
        if hwnd in self.windows:
            self.foreground_hwnd = hwnd

    def show_window(self, hwnd: int, cmd: int) -> bool:
        win = self.windows.get(hwnd)
        if win:
            if cmd == SW_RESTORE:
                win.is_iconic = False
            elif cmd in (SW_SHOWMINIMIZED,):
                win.is_iconic = True
            return True
        return False

    def bring_window_to_top(self, hwnd: int) -> bool:
        return hwnd in self.windows

    def set_active_window(self, hwnd: int) -> int:
        return hwnd if hwnd in self.windows else 0

    def attach_thread_input(self, tid1: int, tid2: int, attach: bool) -> bool:
        return True

    def allow_set_foreground_window(self, pid: int) -> bool:
        return True

    def get_current_thread_id(self) -> int:
        return self.current_thread_id

    def get_dlg_item(self, h_dialog: int, item_id: int) -> int:
        win = self.windows.get(h_dialog)
        if win:
            return win.children.get(item_id, 0)
        return 0

    def send_message(self, hwnd: int, msg: int, wparam: int, lparam: Any) -> int:
        win = self.windows.get(hwnd)
        if not win:
            return 0
        if msg == WM_SETTEXT and isinstance(lparam, str):
            win.text = lparam
            self.recorded_actions.append(
                {"action": "set_text", "hwnd": hwnd, "length": len(lparam)}
            )
            return 1
        if msg == BM_CLICK:
            self.post_message(hwnd, msg, wparam, lparam)
            return 0
        return 0

    def post_message(self, hwnd: int, msg: int, wparam: int, lparam: Any) -> bool:
        win = self.windows.get(hwnd)
        if not win:
            return False

        if msg == BM_CLICK:
            # Login button on KRSDK_MainWindow (ID 10001) clicked -> spawn KRSDK_Login
            if win.hwnd == 10001:
                login_win = FakeWindow(
                    hwnd=2001,
                    pid=win.pid,
                    class_name="KRSDK_Login",
                    title="Login",
                    owner_hwnd=win.owner_hwnd or 19596018,
                    window_rect=(608, 360, 928, 600),
                    client_rect=(0, 0, 305, 202),
                    client_origin=(615, 390),
                )
                u_edit = FakeWindow(hwnd=3001, pid=win.pid, class_name="Edit", title="")
                p_edit = FakeWindow(hwnd=3002, pid=win.pid, class_name="Edit", title="")
                s_btn = FakeWindow(hwnd=3003, pid=win.pid, class_name="Button", title="Login")
                c_btn = FakeWindow(hwnd=3004, pid=win.pid, class_name="Button", title="Cancel")

                login_win.children[ID_EDIT_USERNAME] = 3001
                login_win.children[ID_EDIT_PASSWORD] = 3002
                login_win.children[ID_BTN_SUBMIT] = 3003
                login_win.children[ID_BTN_CANCEL] = 3004

                self.windows[2001] = login_win
                self.windows[3001] = u_edit
                self.windows[3002] = p_edit
                self.windows[3003] = s_btn
                self.windows[3004] = c_btn

                self.recorded_actions.append({"action": "krsdk_login_dialog_opened"})
                return True

            # Submit button on KRSDK_Login (ID 1, hwnd 3003) clicked -> trigger auth simulation
            elif win.hwnd == 3003:
                self.submit_count += 1
                self.recorded_actions.append({"action": "submit_clicked", "count": self.submit_count})
                if self.auth_simulation == "success":
                    msgbox = FakeWindow(
                        hwnd=4001,
                        pid=win.pid,
                        class_name="#32770",
                        title="Success",
                        owner_hwnd=2001,
                    )
                    static_lbl = FakeWindow(
                        hwnd=4002,
                        pid=win.pid,
                        class_name="Static",
                        title="Login successful!",
                    )
                    ok_btn = FakeWindow(
                        hwnd=4003,
                        pid=win.pid,
                        class_name="Button",
                        title="OK",
                    )
                    msgbox.children[0xFFFF] = 4002
                    msgbox.children[IDCANCEL] = 4003
                    self.windows[4001] = msgbox
                    self.windows[4002] = static_lbl
                    self.windows[4003] = ok_btn
                elif self.auth_simulation == "error":
                    msgbox = FakeWindow(
                        hwnd=4001,
                        pid=win.pid,
                        class_name="#32770",
                        title="Error",
                        owner_hwnd=2001,
                    )
                    static_lbl = FakeWindow(
                        hwnd=4002,
                        pid=win.pid,
                        class_name="Static",
                        title=self.error_message,
                    )
                    ok_btn = FakeWindow(
                        hwnd=4003,
                        pid=win.pid,
                        class_name="Button",
                        title="OK",
                    )
                    msgbox.children[0xFFFF] = 4002
                    msgbox.children[IDCANCEL] = 4003
                    self.windows[4001] = msgbox
                    self.windows[4002] = static_lbl
                    self.windows[4003] = ok_btn
                return True

            elif win.hwnd == 4003:
                msgbox = self.windows.get(4001)
                if msgbox:
                    was_success = "success" in msgbox.title.lower()
                    self.windows.pop(4001, None)
                    self.windows.pop(4002, None)
                    self.windows.pop(4003, None)
                    if was_success:
                        self.windows.pop(2001, None)
                        self.windows.pop(3001, None)
                        self.windows.pop(3002, None)
                        self.windows.pop(3003, None)
                        self.windows.pop(3004, None)
                        for hw, candidate in list(self.windows.items()):
                            if candidate.class_name == "KRSDK_MainWindow":
                                self.windows.pop(hw, None)
                return True

        if msg in (WM_COMMAND, WM_CLOSE):
            # Dismissing MessageBox
            if win.class_name == "#32770":
                was_success = "success" in win.title.lower()
                self.windows.pop(hwnd, None)
                if was_success:
                    # Destroy KRSDK login & main windows
                    self.windows.pop(2001, None)
                    self.windows.pop(3001, None)
                    self.windows.pop(3002, None)
                    self.windows.pop(3003, None)
                    self.windows.pop(3004, None)
                    # Close main window as well
                    for hw, w in list(self.windows.items()):
                        if w.class_name == "KRSDK_MainWindow":
                            self.windows.pop(hw, None)
                return True

        return True

    def send_mouse_click(self, screen_x: int, screen_y: int) -> bool:
        if self.foreground_race_during_click:
            self.foreground_hwnd = 99999
        self.recorded_actions.append(
            {"action": "mouse_click", "screen_x": screen_x, "screen_y": screen_y}
        )
        return True

    def send_key_press(self, vk_code: int) -> bool:
        if self.foreground_race_during_key:
            self.foreground_hwnd = 99999
        self.recorded_actions.append({"action": "key_press", "vk_code": vk_code})
        return True

    def grab_screen(self, bbox: Tuple[int, int, int, int]) -> Any:
        if not self.pillow_available:
            raise RuntimeError(
                "Pillow is not installed. Pillow is an optional dependency required for screenshots. "
                "Install it with 'pip install pillow'."
            )
        if self.foreground_race_during_screenshot:
            self.foreground_hwnd = 99999

        # Return a mock PIL Image object
        class FakeImage:
            def save(self, path: str, format: str = "PNG") -> None:
                Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake_image_bytes")

        return FakeImage()


# -----------------------------------------------------------------------------
# Controller Business Logic
# -----------------------------------------------------------------------------
class PgrWindowController:
    """Manages discovery and operations on PGR.exe and its associated windows."""

    def __init__(self, driver: Optional[Win32Driver] = None) -> None:
        self.driver = driver or LiveWin32Driver()

    def discover(self) -> Dict[str, Any]:
        """Resolves PGR.exe PID and all associated windows freshly.

        Never reuses or caches HWNDs or PIDs across invocations.
        """
        h_desk = None
        try:
            h_desk = self.driver.open_input_desktop()
        except Exception:
            pass

        raw_windows: List[Dict[str, Any]] = []

        def enum_callback(hwnd: int, _lparam: int) -> int:
            if not self.driver.is_window(hwnd):
                return 1
            _tid, pid = self.driver.get_window_thread_process_id(hwnd)
            if not pid:
                return 1

            path = self.driver.get_process_image_path(pid)
            if not path or not path.lower().endswith("\\pgr.exe"):
                return 1

            cls_name = self.driver.get_class_name(hwnd)
            title = self.driver.get_window_text(hwnd)
            owner = self.driver.get_window_owner(hwnd)
            visible = self.driver.is_window_visible(hwnd)
            iconic = self.driver.is_iconic(hwnd)

            w_left, w_top, w_right, w_bottom = self.driver.get_window_rect(hwnd)
            _c_left, _c_top, c_width, c_height = self.driver.get_client_rect(hwnd)
            s_left, s_top = self.driver.client_to_screen(hwnd, 0, 0)

            raw_windows.append(
                {
                    "hwnd": hwnd,
                    "pid": pid,
                    "path": path,
                    "class_name": cls_name,
                    "title": title,
                    "owner_hwnd": owner,
                    "is_visible": visible,
                    "is_iconic": iconic,
                    "window_rect": {
                        "left": w_left,
                        "top": w_top,
                        "right": w_right,
                        "bottom": w_bottom,
                        "width": w_right - w_left,
                        "height": w_bottom - w_top,
                    },
                    "client_rect": {
                        "left": s_left,
                        "top": s_top,
                        "right": s_left + c_width,
                        "bottom": s_top + c_height,
                        "width": c_width,
                        "height": c_height,
                    },
                }
            )
            return 1

        try:
            if h_desk:
                self.driver.enum_desktop_windows(h_desk, enum_callback)
            else:
                self.driver.enum_windows(enum_callback)
        finally:
            if h_desk:
                self.driver.close_desktop(h_desk)

        # Structure the found windows
        pgr_pid = None
        pgr_path = None
        game_win = None
        krsdk_main_win = None
        krsdk_login_win = None
        message_box_win = None

        # Prioritize matching process
        for w in raw_windows:
            if w["class_name"] == "UnityWndClass" and (
                w["owner_hwnd"] == 0 or w["owner_hwnd"] is None
            ):
                game_win = w
                pgr_pid = w["pid"]
                pgr_path = w["path"]
                break

        if not game_win and raw_windows:
            # Fallback if game window class name differs or PID found from other window
            game_candidate = next(
                (w for w in raw_windows if w["class_name"] == "UnityWndClass"),
                None,
            )
            if game_candidate:
                game_win = game_candidate
                pgr_pid = game_candidate["pid"]
                pgr_path = game_candidate["path"]
            else:
                pgr_pid = raw_windows[0]["pid"]
                pgr_path = raw_windows[0]["path"]

        # Resolve only the same-PID ownership chain rooted at the game window.
        # This rejects unrelated dialogs created by another component in PGR.exe.
        if game_win and pgr_pid:
            game_hwnd = game_win["hwnd"]
            krsdk_main_win = next(
                (
                    w
                    for w in raw_windows
                    if w["pid"] == pgr_pid
                    and w["class_name"] == "KRSDK_MainWindow"
                    and w["owner_hwnd"] == game_hwnd
                ),
                None,
            )

            login_owners = {game_hwnd}
            if krsdk_main_win:
                login_owners.add(krsdk_main_win["hwnd"])
            krsdk_login_win = next(
                (
                    w
                    for w in raw_windows
                    if w["pid"] == pgr_pid
                    and w["class_name"] == "KRSDK_Login"
                    and w["owner_hwnd"] in login_owners
                ),
                None,
            )

            dialog_owners = set(login_owners)
            if krsdk_login_win:
                dialog_owners.add(krsdk_login_win["hwnd"])
            message_box_win = next(
                (
                    w
                    for w in raw_windows
                    if w["pid"] == pgr_pid
                    and w["class_name"] == "#32770"
                    and w["owner_hwnd"] in dialog_owners
                ),
                None,
            )

        fg_hwnd = self.driver.get_foreground_window()
        fg_tid, fg_pid = (
            self.driver.get_window_thread_process_id(fg_hwnd)
            if fg_hwnd
            else (0, 0)
        )

        return {
            "process": {
                "pid": pgr_pid,
                "name": "PGR.exe" if pgr_pid else None,
                "path": pgr_path,
                "found": bool(pgr_pid),
            },
            "windows": {
                "game": game_win,
                "krsdk_main": krsdk_main_win,
                "krsdk_login": krsdk_login_win,
                "message_box": message_box_win,
            },
            "foreground": {
                "hwnd": fg_hwnd,
                "pid": fg_pid,
                "matches_game": bool(game_win and fg_hwnd == game_win["hwnd"]),
                "matches_krsdk": bool(
                    (krsdk_main_win and fg_hwnd == krsdk_main_win["hwnd"])
                    or (krsdk_login_win and fg_hwnd == krsdk_login_win["hwnd"])
                    or (message_box_win and fg_hwnd == message_box_win["hwnd"])
                ),
            },
        }

    def status(self) -> Dict[str, Any]:
        """Returns the full status inspection JSON."""
        return self.discover()

    def focus(self, target: str = "game", timeout: float = 3.0) -> Dict[str, Any]:
        """Brings the target window to foreground and verifies the result with timeout."""
        data = self.discover()
        if not data["process"]["found"]:
            return {
                "success": False,
                "error": "PROCESS_NOT_FOUND",
                "message": "PGR.exe process was not found.",
            }

        target_win = None
        if target == "game":
            target_win = data["windows"]["game"]
        elif target in ("krsdk", "krsdk_main"):
            target_win = (
                data["windows"]["krsdk_login"]
                or data["windows"]["krsdk_main"]
                or data["windows"]["message_box"]
            )
        elif target == "krsdk_login":
            target_win = data["windows"]["krsdk_login"]
        elif target == "message_box":
            target_win = data["windows"]["message_box"]
        else:
            return {
                "success": False,
                "error": "INVALID_TARGET",
                "message": f"Target '{target}' is not recognized. Use game, krsdk_main, krsdk_login, or message_box.",
            }

        if not target_win:
            return {
                "success": False,
                "error": "WINDOW_NOT_FOUND",
                "message": f"Target window '{target}' was not found for PGR.exe.",
            }

        target_hwnd = target_win["hwnd"]

        # Restore if minimized
        if target_win.get("is_iconic"):
            self.driver.show_window(target_hwnd, SW_RESTORE)

        # Multi-tiered Win32 foreground activation
        cur_tid = self.driver.get_current_thread_id()
        fg_hwnd = self.driver.get_foreground_window()
        fg_tid, _ = (
            self.driver.get_window_thread_process_id(fg_hwnd)
            if fg_hwnd
            else (0, 0)
        )
        target_tid, target_pid = self.driver.get_window_thread_process_id(
            target_hwnd
        )

        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = self.driver.attach_thread_input(cur_tid, fg_tid, True)

        try:
            self.driver.allow_set_foreground_window(-1)
            self.driver.switch_to_this_window(target_hwnd, True)
            self.driver.set_foreground_window(target_hwnd)
            self.driver.bring_window_to_top(target_hwnd)
            self.driver.set_active_window(target_hwnd)
        finally:
            if attached:
                self.driver.attach_thread_input(cur_tid, fg_tid, False)

        # Poll with timeout to verify foreground activation
        start_time = time.monotonic()
        matched = False
        curr_fg = 0
        while time.monotonic() - start_time < timeout:
            curr_fg = self.driver.get_foreground_window()
            if curr_fg == target_hwnd:
                matched = True
                break
            time.sleep(0.05)

        elapsed_ms = round((time.monotonic() - start_time) * 1000, 1)

        return {
            "success": matched,
            "target": target,
            "target_hwnd": target_hwnd,
            "target_class": target_win["class_name"],
            "foreground_hwnd": curr_fg,
            "matched": matched,
            "elapsed_ms": elapsed_ms,
        }

    def screenshot(
        self,
        output_path: Optional[str] = None,
        allow_krsdk_foreground: bool = False,
    ) -> Dict[str, Any]:
        """Captures visible client rect of PGR.exe if foreground matches.

        Verifies game foreground before and after capture.
        Documents Win32 race limitation explicitly.
        """
        data = self.discover()
        if not data["process"]["found"]:
            return {
                "success": False,
                "error": "PROCESS_NOT_FOUND",
                "message": "PGR.exe process was not found.",
            }

        game_win = data["windows"]["game"]
        if not game_win:
            return {
                "success": False,
                "error": "GAME_WINDOW_NOT_FOUND",
                "message": "UnityWndClass top-level game window was not found.",
            }

        game_hwnd = game_win["hwnd"]

        if not game_win["is_visible"]:
            return {
                "success": False,
                "error": "WINDOW_NOT_VISIBLE",
                "message": f"Game window {game_hwnd} is not visible.",
            }

        if game_win["is_iconic"]:
            return {
                "success": False,
                "error": "WINDOW_MINIMIZED",
                "message": f"Game window {game_hwnd} is minimized.",
            }

        c_rect = game_win["client_rect"]
        if c_rect["width"] <= 0 or c_rect["height"] <= 0:
            return {
                "success": False,
                "error": "INVALID_CLIENT_DIMENSIONS",
                "message": f"Game window client dimensions are non-positive: {c_rect['width']}x{c_rect['height']}.",
            }

        # Check foreground before
        fg_before = self.driver.get_foreground_window()
        fg_valid = fg_before == game_hwnd or (
            allow_krsdk_foreground and data["foreground"]["matches_krsdk"]
        )
        if not fg_valid:
            return {
                "success": False,
                "error": "FOREGROUND_MISMATCH_PRE",
                "message": f"Game window {game_hwnd} is not in the foreground before capture (foreground HWND: {fg_before}).",
                "foreground_hwnd": fg_before,
                "target_hwnd": game_hwnd,
            }

        # Resolve output path
        if output_path:
            out_file = Path(output_path).resolve()
        else:
            ts = time.strftime("%Y%m%d_%H%M%S")
            out_file = (
                Path(".runtime") / "screenshots" / f"pgr_client_{ts}.png"
            ).resolve()

        out_file.parent.mkdir(parents=True, exist_ok=True)

        bbox = (
            c_rect["left"],
            c_rect["top"],
            c_rect["right"],
            c_rect["bottom"],
        )

        try:
            img = self.driver.grab_screen(bbox)
        except RuntimeError as e:
            return {
                "success": False,
                "error": "DEPENDENCY_MISSING",
                "message": str(e),
            }
        except Exception as e:
            return {
                "success": False,
                "error": "CAPTURE_FAILED",
                "message": f"Screen grab failed: {e}",
            }

        # Check foreground immediately after capture
        fg_after = self.driver.get_foreground_window()
        if fg_after != fg_before:
            return {
                "success": False,
                "error": "FOREGROUND_RACE_POST",
                "message": f"Foreground window changed during capture from {fg_before} to {fg_after}. Capture rejected.",
                "foreground_before": fg_before,
                "foreground_after": fg_after,
                "target_hwnd": game_hwnd,
            }

        try:
            img.save(str(out_file), format="PNG")
        except Exception as e:
            return {
                "success": False,
                "error": "FILE_SAVE_FAILED",
                "message": f"Failed to save image to {out_file}: {e}",
            }

        return {
            "success": True,
            "output_path": str(out_file),
            "target_hwnd": game_hwnd,
            "client_rect": c_rect,
            "race_limitation_warning": (
                "Win32 screen capture samples visible screen pixels. While game "
                "foreground was verified immediately before and after capture, "
                "Win32 desktop capture cannot atomically guarantee that another "
                "topmost/occluding window or rapid focus race did not occur between checks."
            ),
        }

    def login_krsdk(
        self, timeout: float = 10.0, poll_interval: float = 0.1
    ) -> Dict[str, Any]:
        """Automates KRSDK login modal dialog.

        Credentials are read exclusively from ASCNET_USERNAME and ASCNET_PASSWORD.
        Avoids password logging. Operates exact child IDs 10001/1001/1002/1.
        Does NOT retry on timeout.
        """
        username = os.environ.get("ASCNET_USERNAME", "")
        password = os.environ.get("ASCNET_PASSWORD", "")

        if not username.strip() or password == "":
            return {
                "success": False,
                "error": "MISSING_CREDENTIALS",
                "message": (
                    "Both ASCNET_USERNAME and ASCNET_PASSWORD environment variables "
                    "must be set and non-empty."
                ),
            }

        data = self.discover()
        if not data["process"]["found"]:
            return {
                "success": False,
                "error": "PROCESS_NOT_FOUND",
                "message": "PGR.exe process was not found.",
            }

        pgr_pid = data["process"]["pid"]
        krsdk_main = data["windows"]["krsdk_main"]
        krsdk_login = data["windows"]["krsdk_login"]
        pending_message_box = data["windows"]["message_box"]

        if pending_message_box:
            return {
                "success": False,
                "error": "PENDING_DIALOG",
                "message": (
                    "A KRSDK result dialog is already open. Resolve it before "
                    "submitting login again."
                ),
                "observed_dialog": pending_message_box.get("title", ""),
            }

        if not krsdk_main and not krsdk_login:
            return {
                "success": False,
                "error": "KRSDK_WINDOW_NOT_FOUND",
                "message": "Neither KRSDK_MainWindow nor KRSDK_Login was found for PGR.exe.",
            }

        # Step 1: If KRSDK_Login is not yet open, click Login button (ID 10001) on KRSDK_MainWindow
        if not krsdk_login and krsdk_main:
            main_hwnd = krsdk_main["hwnd"]
            btn_login_hwnd = self.driver.get_dlg_item(main_hwnd, ID_BTN_LOGIN)
            if not btn_login_hwnd:
                return {
                    "success": False,
                    "error": "CONTROL_NOT_FOUND",
                    "message": f"Login button (ID {ID_BTN_LOGIN}) not found on KRSDK_MainWindow.",
                }

            _btn_tid, btn_pid = self.driver.get_window_thread_process_id(
                btn_login_hwnd
            )
            if btn_pid != pgr_pid:
                return {
                    "success": False,
                    "error": "PID_MISMATCH",
                    "message": f"Login button PID {btn_pid} does not match PGR.exe PID {pgr_pid}.",
                }

            # Post click message to avoid blocking caller thread
            if not self.driver.post_message(btn_login_hwnd, BM_CLICK, 0, 0):
                return {
                    "success": False,
                    "error": "MESSAGE_DISPATCH_FAILED",
                    "message": "Failed to dispatch the KRSDK Login button click.",
                }

            # Wait for KRSDK_Login to appear
            start_wait = time.monotonic()
            while time.monotonic() - start_wait < min(timeout, 3.0):
                time.sleep(poll_interval)
                sub_data = self.discover()
                if sub_data["windows"]["krsdk_login"]:
                    krsdk_login = sub_data["windows"]["krsdk_login"]
                    break

        if not krsdk_login:
            return {
                "success": False,
                "error": "LOGIN_DIALOG_NOT_OPENED",
                "message": "KRSDK_Login dialog did not appear after clicking Login button.",
            }

        login_hwnd = krsdk_login["hwnd"]
        _login_tid, login_pid = self.driver.get_window_thread_process_id(
            login_hwnd
        )
        if login_pid != pgr_pid:
            return {
                "success": False,
                "error": "PID_MISMATCH",
                "message": f"KRSDK_Login PID {login_pid} does not match PGR.exe PID {pgr_pid}.",
            }

        # Step 2: Locate child controls 1001 (username), 1002 (password), 1 (submit)
        uname_ctrl = self.driver.get_dlg_item(login_hwnd, ID_EDIT_USERNAME)
        pwd_ctrl = self.driver.get_dlg_item(login_hwnd, ID_EDIT_PASSWORD)
        submit_ctrl = self.driver.get_dlg_item(login_hwnd, ID_BTN_SUBMIT)

        if not uname_ctrl:
            return {
                "success": False,
                "error": "CONTROL_NOT_FOUND",
                "message": f"Username edit control (ID {ID_EDIT_USERNAME}) not found.",
            }
        if not pwd_ctrl:
            return {
                "success": False,
                "error": "CONTROL_NOT_FOUND",
                "message": f"Password edit control (ID {ID_EDIT_PASSWORD}) not found.",
            }
        if not submit_ctrl:
            return {
                "success": False,
                "error": "CONTROL_NOT_FOUND",
                "message": f"Submit button control (ID {ID_BTN_SUBMIT}) not found.",
            }

        # Verify same-PID for each control
        for cid, chwnd in [
            (ID_EDIT_USERNAME, uname_ctrl),
            (ID_EDIT_PASSWORD, pwd_ctrl),
            (ID_BTN_SUBMIT, submit_ctrl),
        ]:
            _, c_pid = self.driver.get_window_thread_process_id(chwnd)
            if c_pid != pgr_pid:
                return {
                    "success": False,
                    "error": "CONTROL_PID_MISMATCH",
                    "message": f"Control {cid} PID {c_pid} does not match PGR.exe PID {pgr_pid}.",
                }

        # Step 3: Populate credentials (do NOT log password!)
        if not self.driver.send_message(uname_ctrl, WM_SETTEXT, 0, username):
            return {
                "success": False,
                "error": "MESSAGE_DISPATCH_FAILED",
                "message": "Failed to populate the KRSDK username control.",
            }
        if not self.driver.send_message(pwd_ctrl, WM_SETTEXT, 0, password):
            return {
                "success": False,
                "error": "MESSAGE_DISPATCH_FAILED",
                "message": "Failed to populate the KRSDK password control.",
            }

        # Step 4: Dispatch submit via PostMessage to avoid hanging on modal MessageBox
        if not self.driver.post_message(submit_ctrl, BM_CLICK, 0, 0):
            return {
                "success": False,
                "error": "MESSAGE_DISPATCH_FAILED",
                "message": "Failed to dispatch the KRSDK submit button click.",
            }

        # Step 5: Observe outcome dialog / window dismissal
        # We do NOT repeat submission on timeout.
        start_obs = time.monotonic()
        while time.monotonic() - start_obs < timeout:
            time.sleep(poll_interval)
            obs_data = self.discover()
            msgbox = obs_data["windows"]["message_box"]

            if msgbox:
                msg_title = msgbox.get("title", "")
                msgbox_hwnd = msgbox["hwnd"]

                # Extract dialog text if possible from static child
                static_text = ""
                static_hwnd = self.driver.get_dlg_item(msgbox_hwnd, 0xFFFF)
                if static_hwnd:
                    static_text = self.driver.get_window_text(static_hwnd)

                # Click the actual OK child. KRSDK's native dialog labels the
                # button "OK" but has been observed using control ID 2; accept
                # either standard ID rather than posting to the parent window.
                ok_ctrl = self.driver.get_dlg_item(msgbox_hwnd, IDOK)
                if not ok_ctrl:
                    ok_ctrl = self.driver.get_dlg_item(msgbox_hwnd, IDCANCEL)
                dismissed = bool(ok_ctrl)
                if ok_ctrl:
                    # BM_CLICK conventionally returns zero, so completion is
                    # verified by the window-destruction checks below.
                    self.driver.send_message(ok_ctrl, BM_CLICK, 0, 0)

                if "success" in msg_title.lower():
                    if not dismissed:
                        return {
                            "success": False,
                            "status": "LOGIN_ACCEPTED_UNDISMISSED",
                            "error": "DIALOG_DISMISS_FAILED",
                            "username": username,
                            "message": static_text or "Login succeeded, but the result dialog could not be dismissed.",
                            "observed_dialog": msg_title,
                        }

                    # Require the dialog and both KRSDK windows to close before
                    # reporting an unattended login as complete.
                    close_start = time.monotonic()
                    main_hwnd = krsdk_main["hwnd"] if krsdk_main else 0
                    while time.monotonic() - close_start < min(timeout, 5.0):
                        if (
                            not self.driver.is_window(msgbox_hwnd)
                            and not self.driver.is_window(login_hwnd)
                            and (not main_hwnd or not self.driver.is_window(main_hwnd))
                        ):
                            return {
                                "success": True,
                                "status": "LOGIN_SUCCESS",
                                "username": username,
                                "message": static_text or "Login successful",
                                "observed_dialog": msg_title,
                            }
                        time.sleep(0.05)

                    return {
                        "success": False,
                        "status": "LOGIN_ACCEPTED_UNDISMISSED",
                        "error": "DIALOG_CLEANUP_TIMEOUT",
                        "username": username,
                        "message": (
                            static_text
                            or "Login succeeded, but KRSDK windows did not close before timeout."
                        ),
                        "observed_dialog": msg_title,
                    }
                else:
                    return {
                        "success": False,
                        "status": "LOGIN_FAILED",
                        "error": "AUTH_FAILED",
                        "username": username,
                        "message": (
                            static_text or f"Login failed: {msg_title}"
                        )
                        + ("" if dismissed else " The result dialog could not be dismissed."),
                        "observed_dialog": msg_title,
                    }

            # Check if windows closed without explicit MessageBox or already closed
            main_hwnd = krsdk_main["hwnd"] if krsdk_main else 0
            if not self.driver.is_window(login_hwnd) and (
                not main_hwnd or not self.driver.is_window(main_hwnd)
            ):
                return {
                    "success": True,
                    "status": "LOGIN_SUCCESS",
                    "username": username,
                    "message": "KRSDK login window closed.",
                }

        # Timeout reached: outcome is uncertain. Do NOT repeat submission.
        return {
            "success": False,
            "status": "TIMEOUT_UNCERTAIN",
            "error": "SUBMISSION_TIMEOUT",
            "username": username,
            "message": (
                f"Login submission was dispatched for user '{username}', but neither "
                "confirmation dialog, error dialog, nor window closure was observed "
                f"within {timeout}s timeout. Outcome is uncertain. Submission was NOT retried."
            ),
        }

    def click(self, x: int, y: int) -> Dict[str, Any]:
        """Dispatches mouse click at game client-relative (x, y).

        Verifies game foreground before and after input. No implicit retry.
        """
        data = self.discover()
        if not data["process"]["found"]:
            return {
                "success": False,
                "error": "PROCESS_NOT_FOUND",
                "message": "PGR.exe process was not found.",
            }

        game_win = data["windows"]["game"]
        if not game_win:
            return {
                "success": False,
                "error": "GAME_WINDOW_NOT_FOUND",
                "message": "UnityWndClass game window was not found.",
            }

        game_hwnd = game_win["hwnd"]

        # 1. Verify foreground BEFORE input
        fg_before = self.driver.get_foreground_window()
        if fg_before != game_hwnd:
            return {
                "success": False,
                "error": "FOREGROUND_MISMATCH_PRE",
                "message": (
                    f"Game window {game_hwnd} is not in the foreground before click. "
                    f"Current foreground is {fg_before}. Input aborted."
                ),
                "foreground_hwnd": fg_before,
                "target_hwnd": game_hwnd,
            }

        # 2. Check coordinate bounds
        c_rect = game_win["client_rect"]
        c_w = c_rect["width"]
        c_h = c_rect["height"]
        if x < 0 or y < 0 or x >= c_w or y >= c_h:
            return {
                "success": False,
                "error": "OUT_OF_BOUNDS",
                "message": (
                    f"Coordinates ({x}, {y}) are outside client bounds (0, 0, {c_w}, {c_h})."
                ),
                "client_width": c_w,
                "client_height": c_h,
            }

        # 3. Convert client relative to screen coordinates
        screen_x, screen_y = self.driver.client_to_screen(game_hwnd, x, y)

        # 4. Immediate pre-check right before SendInput
        if self.driver.get_foreground_window() != game_hwnd:
            return {
                "success": False,
                "error": "FOREGROUND_LOST_RACE",
                "message": "Foreground lost immediately prior to SendInput. Input aborted.",
            }

        # 5. SendInput
        sent = self.driver.send_mouse_click(screen_x, screen_y)

        # 6. Post-check right after SendInput
        fg_after = self.driver.get_foreground_window()
        fg_retained = fg_after == game_hwnd

        return {
            "success": sent and fg_retained,
            "command": "click",
            "client_coords": {"x": x, "y": y},
            "screen_coords": {"x": screen_x, "y": screen_y},
            "target_hwnd": game_hwnd,
            "foreground_retained": fg_retained,
            "foreground_after": fg_after,
            "race_limitation_warning": (
                "SendInput dispatches into the Windows input stream without HWND binding. "
                "Foreground verification was performed before and after dispatch. "
                "No implicit retry was performed."
            ),
        }

    def key(self, key_name: str) -> Dict[str, Any]:
        """Dispatches key press to the game window.

        Verifies game foreground before and after input. No implicit retry.
        """
        data = self.discover()
        if not data["process"]["found"]:
            return {
                "success": False,
                "error": "PROCESS_NOT_FOUND",
                "message": "PGR.exe process was not found.",
            }

        game_win = data["windows"]["game"]
        if not game_win:
            return {
                "success": False,
                "error": "GAME_WINDOW_NOT_FOUND",
                "message": "UnityWndClass game window was not found.",
            }

        game_hwnd = game_win["hwnd"]

        vk_code = VK_MAP.get(key_name.lower())
        if vk_code is None:
            return {
                "success": False,
                "error": "UNSUPPORTED_KEY",
                "message": f"Key '{key_name}' is not supported. Supported keys include: space, enter, escape, tab, up, down, left, right, a-z, 0-9.",
            }

        # 1. Verify foreground BEFORE input
        fg_before = self.driver.get_foreground_window()
        if fg_before != game_hwnd:
            return {
                "success": False,
                "error": "FOREGROUND_MISMATCH_PRE",
                "message": (
                    f"Game window {game_hwnd} is not in the foreground before key input. "
                    f"Current foreground is {fg_before}. Input aborted."
                ),
                "foreground_hwnd": fg_before,
                "target_hwnd": game_hwnd,
            }

        # 2. Immediate pre-check right before SendInput
        if self.driver.get_foreground_window() != game_hwnd:
            return {
                "success": False,
                "error": "FOREGROUND_LOST_RACE",
                "message": "Foreground lost immediately prior to SendInput. Input aborted.",
            }

        # 3. SendInput
        sent = self.driver.send_key_press(vk_code)

        # 4. Post-check right after SendInput
        fg_after = self.driver.get_foreground_window()
        fg_retained = fg_after == game_hwnd

        return {
            "success": sent and fg_retained,
            "command": "key",
            "key": key_name.lower(),
            "vk_code": hex(vk_code),
            "target_hwnd": game_hwnd,
            "foreground_retained": fg_retained,
            "foreground_after": fg_after,
            "race_limitation_warning": (
                "SendInput dispatches into the Windows input stream without HWND binding. "
                "Foreground verification was performed before and after dispatch. "
                "No implicit retry was performed."
            ),
        }


# -----------------------------------------------------------------------------
# CLI Parser & Entry Point
# -----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="External Windows control CLI for Punishing: Gray Raven (PGR.exe)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status / list
    subparsers.add_parser(
        "status", help="Inspect and list PGR process, windows, rects, and foreground"
    )
    subparsers.add_parser(
        "list", help="Alias for status"
    )

    # focus
    focus_parser = subparsers.add_parser(
        "focus", help="Activate target window into foreground with verification"
    )
    focus_parser.add_argument(
        "--target",
        choices=["game", "krsdk", "krsdk_main", "krsdk_login", "message_box"],
        default="game",
        help="Target window to activate (default: game)",
    )
    focus_parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Timeout in seconds to verify foreground activation (default: 3.0)",
    )

    # screenshot
    screenshot_parser = subparsers.add_parser(
        "screenshot", help="Capture visible client rect of PGR.exe if foreground"
    )
    screenshot_parser.add_argument(
        "--out",
        dest="output_path",
        help="Destination PNG file path (default: .runtime/screenshots/pgr_client_<ts>.png)",
    )
    screenshot_parser.add_argument(
        "--allow-krsdk-foreground",
        action="store_true",
        help="Allow capture if KRSDK modal is in foreground",
    )

    # login
    login_parser = subparsers.add_parser(
        "login",
        help="Automate KRSDK modal login from ASCNET_USERNAME and ASCNET_PASSWORD env",
    )
    login_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Timeout in seconds to observe outcome (default: 10.0)",
    )

    # click
    click_parser = subparsers.add_parser(
        "click", help="Dispatch game-client-relative mouse click via SendInput"
    )
    click_parser.add_argument(
        "--x", type=int, required=True, help="Client X coordinate"
    )
    click_parser.add_argument(
        "--y", type=int, required=True, help="Client Y coordinate"
    )

    # key
    key_parser = subparsers.add_parser(
        "key", help="Dispatch key press via SendInput"
    )
    key_parser.add_argument(
        "--key",
        type=str,
        required=True,
        help="Key name (e.g. space, enter, escape)",
    )

    return parser


def main(argv: Optional[List[str]] = None, driver: Optional[Win32Driver] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    controller = PgrWindowController(driver=driver)

    if args.command in ("status", "list"):
        result = controller.status()
    elif args.command == "focus":
        result = controller.focus(target=args.target, timeout=args.timeout)
    elif args.command == "screenshot":
        result = controller.screenshot(
            output_path=args.output_path,
            allow_krsdk_foreground=args.allow_krsdk_foreground,
        )
    elif args.command == "login":
        result = controller.login_krsdk(timeout=args.timeout)
    elif args.command == "click":
        result = controller.click(x=args.x, y=args.y)
    elif args.command == "key":
        result = controller.key(key_name=args.key)
    else:
        result = {
            "success": False,
            "error": "UNKNOWN_COMMAND",
            "message": f"Unknown command: {args.command}",
        }

    print(json.dumps(result, indent=2))
    return 0 if result.get("success", True) else 1


if __name__ == "__main__":
    sys.exit(main())
