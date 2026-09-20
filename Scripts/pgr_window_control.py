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
import math
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

# Thread DPI awareness context pseudo-handle values (winuser.h). These are
# passed to SetThreadDpiAwarenessContext; the function returns the previous
# opaque context value which must be passed back verbatim to restore it.
DPI_AWARENESS_CONTEXT_UNAWARE = -1
DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
DPI_AWARENESS_CONTEXT_UNAWARE_GDISCALED = -5


class PhysicalRectUnavailableError(RuntimeError):
    """Raised when reliable physical client-rectangle measurement is unavailable."""

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

    def supports_thread_dpi_awareness_context(self) -> bool:
        """True if SetThreadDpiAwarenessContext is available (Win10 1703+)."""
        raise NotImplementedError

    def set_thread_dpi_awareness_context(self, context: int) -> int:
        """Applies a DPI awareness context to the calling thread.

        Returns the previous opaque context value (pass it back to restore),
        or 0 if the call failed.
        """
        raise NotImplementedError

    def get_dpi_for_window(self, hwnd: int) -> int:
        """Returns the DPI of the window's display, or 0 if unavailable."""
        raise NotImplementedError

    def get_system_metrics(self, index: int) -> int:
        raise NotImplementedError

    def get_physical_client_rect(self, hwnd: int) -> Dict[str, Any]:
        """Measures a window's client rectangle in physical device pixels.

        A DPI-unaware process receives virtualized (scaled-down) coordinates
        from GetClientRect/ClientToScreen/GetSystemMetrics, while Pillow's
        ImageGrab consumes physical device pixels. Feeding virtualized rects
        to ImageGrab produces a literal top-left crop of the frame. To obtain
        the physical rectangle this method temporarily raises the calling
        thread's DPI awareness to a per-monitor context, measures, and then
        restores the prior context in all cases.

        Returns a dict with the physical client screen rect (left/top/right/
        bottom/width/height), the window dpi, the awareness context used, and
        the physical virtual-screen bounds used for containment validation.

        Raises PhysicalRectUnavailableError when the awareness-context API is
        missing or rejects a per-monitor context; in that case no reliable
        physical measurement exists and callers must fail closed. Any other
        exception propagates after the context is restored.
        """
        if not self.supports_thread_dpi_awareness_context():
            raise PhysicalRectUnavailableError(
                "SetThreadDpiAwarenessContext is unavailable on this system; "
                "physical client rect measurement is impossible."
            )

        previous_context = 0
        context_name = ""
        for context_value, name in (
            (
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
                "PER_MONITOR_AWARE_V2",
            ),
            (
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE,
                "PER_MONITOR_AWARE",
            ),
        ):
            previous_context = self.set_thread_dpi_awareness_context(
                context_value
            )
            if previous_context:
                context_name = name
                break
        if not previous_context:
            raise PhysicalRectUnavailableError(
                "SetThreadDpiAwarenessContext rejected per-monitor awareness "
                "contexts; physical client rect measurement is impossible."
            )

        try:
            c_left, c_top, c_right, c_bottom = self.get_client_rect(hwnd)
            s_left, s_top = self.client_to_screen(hwnd, 0, 0)
            vs_left = self.get_system_metrics(SM_XVIRTUALSCREEN)
            vs_top = self.get_system_metrics(SM_YVIRTUALSCREEN)
            vs_width = self.get_system_metrics(SM_CXVIRTUALSCREEN)
            vs_height = self.get_system_metrics(SM_CYVIRTUALSCREEN)
            dpi = self.get_dpi_for_window(hwnd)
        finally:
            self.set_thread_dpi_awareness_context(previous_context)

        width = c_right - c_left
        height = c_bottom - c_top
        return {
            "left": s_left,
            "top": s_top,
            "right": s_left + width,
            "bottom": s_top + height,
            "width": width,
            "height": height,
            "dpi": dpi,
            "dpi_awareness_context": context_name,
            "virtual_screen": {
                "left": vs_left,
                "top": vs_top,
                "right": vs_left + vs_width,
                "bottom": vs_top + vs_height,
                "width": vs_width,
                "height": vs_height,
            },
        }


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

        # DPI-awareness APIs are only present on Windows 10 1607+/1703+.
        # Configure signatures only when the exports exist so the driver can
        # still be constructed on older systems and fail closed later.
        if hasattr(self.user32, "SetThreadDpiAwarenessContext"):
            self.user32.SetThreadDpiAwarenessContext.restype = ctypes.c_ssize_t
            self.user32.SetThreadDpiAwarenessContext.argtypes = [
                ctypes.c_ssize_t
            ]
        if hasattr(self.user32, "GetDpiForWindow"):
            self.user32.GetDpiForWindow.restype = wintypes.UINT
            self.user32.GetDpiForWindow.argtypes = [wintypes.HWND]

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

    def supports_thread_dpi_awareness_context(self) -> bool:
        return hasattr(self.user32, "SetThreadDpiAwarenessContext")

    def set_thread_dpi_awareness_context(self, context: int) -> int:
        func = getattr(self.user32, "SetThreadDpiAwarenessContext", None)
        if func is None:
            return 0
        return int(func(context) or 0)

    def get_dpi_for_window(self, hwnd: int) -> int:
        func = getattr(self.user32, "GetDpiForWindow", None)
        if func is None:
            return 0
        return int(func(hwnd))

    def get_system_metrics(self, index: int) -> int:
        return int(self.user32.GetSystemMetrics(index))


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
        physical_client_rect: Optional[Tuple[int, int, int, int]] = None,
        physical_client_origin: Optional[Tuple[int, int]] = None,
        dpi: int = 96,
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
        # Physical (device-pixel) geometry reported while the calling thread
        # holds a per-monitor DPI awareness context. Defaults to the virtual
        # geometry, modeling a 96-DPI (100%) display.
        self.physical_client_rect = (
            physical_client_rect
            if physical_client_rect is not None
            else client_rect
        )
        self.physical_client_origin = (
            physical_client_origin
            if physical_client_origin is not None
            else client_origin
        )
        self.dpi = dpi
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

        # DPI-awareness simulation. The CLI starts DPI-unaware; while the
        # thread context is a per-monitor value, rect/metric getters report
        # the physical geometry of a window instead of the virtualized one.
        self.thread_dpi_awareness_context: int = DPI_AWARENESS_CONTEXT_UNAWARE
        self.dpi_context_api_available: bool = True
        self.raise_during_aware_measurement: bool = False
        self.virtual_screen_virtual: Tuple[int, int, int, int] = (
            0,
            0,
            1536,
            960,
        )
        self.virtual_screen_physical: Tuple[int, int, int, int] = (
            0,
            0,
            1920,
            1200,
        )
        self.grabbed_size_override: Optional[Tuple[int, int]] = None

    def _aware_physical(self) -> bool:
        """True when the simulated thread context yields physical pixels."""
        return self.thread_dpi_awareness_context in (
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE,
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
        )

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
        if self.raise_during_aware_measurement and self._aware_physical():
            raise RuntimeError("Simulated failure during physical measurement")
        win = self.windows.get(hwnd)
        if not win:
            return (0, 0, 0, 0)
        if self._aware_physical():
            return win.physical_client_rect
        return win.client_rect

    def client_to_screen(self, hwnd: int, x: int, y: int) -> Tuple[int, int]:
        win = self.windows.get(hwnd)
        if win:
            if self._aware_physical():
                ox, oy = win.physical_client_origin
            else:
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

        self.recorded_actions.append(
            {"action": "grab_screen", "bbox": tuple(bbox)}
        )

        width, height = self.grabbed_size_override or (
            bbox[2] - bbox[0],
            bbox[3] - bbox[1],
        )

        # Return a mock PIL Image object
        class FakeImage:
            def __init__(self, w: int, h: int) -> None:
                self.size = (w, h)

            def save(self, path: str, format: str = "PNG") -> None:
                Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake_image_bytes")

        return FakeImage(width, height)

    def supports_thread_dpi_awareness_context(self) -> bool:
        return self.dpi_context_api_available

    def set_thread_dpi_awareness_context(self, context: int) -> int:
        if not self.dpi_context_api_available:
            return 0
        previous = self.thread_dpi_awareness_context
        self.recorded_actions.append(
            {
                "action": "set_thread_dpi_awareness_context",
                "context": context,
                "previous": previous,
            }
        )
        self.thread_dpi_awareness_context = context
        return previous

    def get_dpi_for_window(self, hwnd: int) -> int:
        win = self.windows.get(hwnd)
        return win.dpi if win else 0

    def get_system_metrics(self, index: int) -> int:
        left, top, width, height = (
            self.virtual_screen_physical
            if self._aware_physical()
            else self.virtual_screen_virtual
        )
        metrics = {
            SM_XVIRTUALSCREEN: left,
            SM_YVIRTUALSCREEN: top,
            SM_CXVIRTUALSCREEN: width,
            SM_CYVIRTUALSCREEN: height,
            SM_CXSCREEN: width,
            SM_CYSCREEN: height,
        }
        return metrics.get(index, 0)


# -----------------------------------------------------------------------------
# UI Action Layout (declarative named actions)
# -----------------------------------------------------------------------------
# A versioned JSON file maps authored action names to normalized client
# anchors plus provenance. Anchors resolve into the same virtualized client
# coordinate space that `click` consumes, so calibrated legacy coordinates
# carry over directly (e.g. 768/1536 = 0.5).
UI_LAYOUT_SCHEMA = "pgr-ui-layout"
UI_LAYOUT_SCHEMA_VERSION = 1
DEFAULT_UI_LAYOUT_PATH = Path(__file__).resolve().with_name("pgr_ui_layout.json")

_LAYOUT_TOP_LEVEL_FIELDS = frozenset(("schema", "schemaVersion", "actions"))
_ACTION_REQUIRED_FIELDS = frozenset(
    ("description", "clientControl", "anchor", "status", "dispatch", "verification")
)
_ACTION_ALLOWED_FIELDS = _ACTION_REQUIRED_FIELDS | frozenset(
    ("calibrationSource",)
)
_ACTION_STATUSES = frozenset(("calibrated", "provisional"))
_ANCHOR_TYPES = frozenset(("normalized_point",))
_ANCHOR_FIELDS = frozenset(("type", "x", "y"))
_DISPATCH_TYPES = frozenset(("click",))
_VERIFICATION_KINDS = frozenset(("none", "screenshot-diff"))
_VERIFICATION_ALLOWED_FIELDS = {
    "none": frozenset(("kind",)),
    "screenshot-diff": frozenset(("kind", "minChangeRatio")),
}


class UiLayoutError(Exception):
    """Structured UI layout load/validation failure.

    Carries a machine-readable code (LAYOUT_NOT_FOUND or LAYOUT_INVALID) so
    callers fail closed with a distinct CLI error payload.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_json_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_action_entry(name: str, entry: Any, source: str) -> None:
    """Validates a single layout action entry; raises UiLayoutError on any defect."""
    prefix = f"UI layout {source} action '{name}'"
    if not isinstance(entry, dict):
        raise UiLayoutError(
            "LAYOUT_INVALID", f"{prefix} must be a JSON object."
        )
    unknown = set(entry) - _ACTION_ALLOWED_FIELDS
    if unknown:
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"{prefix} has unsupported field(s): {sorted(unknown)}.",
        )
    missing = _ACTION_REQUIRED_FIELDS - set(entry)
    if missing:
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"{prefix} is missing required field(s): {sorted(missing)}.",
        )

    for field in ("description", "clientControl"):
        value = entry[field]
        if not isinstance(value, str) or not value.strip():
            raise UiLayoutError(
                "LAYOUT_INVALID",
                f"{prefix} field '{field}' must be a non-empty string.",
            )

    status = entry["status"]
    if not isinstance(status, str) or status not in _ACTION_STATUSES:
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"{prefix} field 'status' must be one of "
            f"{sorted(_ACTION_STATUSES)}; got {status!r}.",
        )

    calibration_source = entry.get("calibrationSource")
    if calibration_source is not None and (
        not isinstance(calibration_source, str)
        or not calibration_source.strip()
    ):
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"{prefix} field 'calibrationSource' must be a non-empty string "
            "when present.",
        )
    if status == "calibrated" and not calibration_source:
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"{prefix} is 'calibrated' but lacks the required "
            "'calibrationSource' provenance.",
        )

    anchor = entry["anchor"]
    if anchor is None:
        # An unset anchor is only meaningful for provisional entries; a
        # calibrated entry without measured coordinates is contradictory and
        # must fail closed.
        if status == "calibrated":
            raise UiLayoutError(
                "LAYOUT_INVALID",
                f"{prefix} is 'calibrated' but its anchor is unset.",
            )
    elif isinstance(anchor, dict):
        if set(anchor) != _ANCHOR_FIELDS:
            raise UiLayoutError(
                "LAYOUT_INVALID",
                f"{prefix} anchor must have exactly the fields "
                f"{sorted(_ANCHOR_FIELDS)}.",
            )
        anchor_type = anchor["type"]
        if not isinstance(anchor_type, str) or anchor_type not in _ANCHOR_TYPES:
            raise UiLayoutError(
                "LAYOUT_INVALID",
                f"{prefix} anchor 'type' must be one of "
                f"{sorted(_ANCHOR_TYPES)}; got {anchor_type!r}.",
            )
        for axis in ("x", "y"):
            value = anchor[axis]
            if (
                not _is_json_number(value)
                or not (0.0 <= value < 1.0)
                or not math.isfinite(value)
            ):
                raise UiLayoutError(
                    "LAYOUT_INVALID",
                    f"{prefix} anchor '{axis}' must be a finite number with "
                    f"0 <= {axis} < 1; got {value!r}.",
                )
    else:
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"{prefix} field 'anchor' must be null or a normalized-point "
            "object.",
        )

    dispatch = entry["dispatch"]
    if (
        not isinstance(dispatch, dict)
        or set(dispatch) != {"type"}
        or not isinstance(dispatch["type"], str)
        or dispatch["type"] not in _DISPATCH_TYPES
    ):
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"{prefix} field 'dispatch' must be an object whose only key is "
            f"'type' with a value in {sorted(_DISPATCH_TYPES)}.",
        )

    verification = entry["verification"]
    kind = verification.get("kind") if isinstance(verification, dict) else None
    if not isinstance(kind, str) or kind not in _VERIFICATION_KINDS:
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"{prefix} field 'verification' must be an object with 'kind' in "
            f"{sorted(_VERIFICATION_KINDS)}.",
        )
    unsupported_verification_fields = (
        set(verification) - _VERIFICATION_ALLOWED_FIELDS[kind]
    )
    if unsupported_verification_fields:
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"{prefix} verification kind '{kind}' does not support "
            f"field(s): {sorted(unsupported_verification_fields)}.",
        )
    if "minChangeRatio" in verification:
        ratio = verification["minChangeRatio"]
        if (
            not _is_json_number(ratio)
            or not (0.0 < ratio <= 1.0)
            or not math.isfinite(ratio)
        ):
            raise UiLayoutError(
                "LAYOUT_INVALID",
                f"{prefix} verification 'minChangeRatio' must be a finite "
                f"number with 0 < minChangeRatio <= 1; got {ratio!r}.",
            )


def load_ui_layout(layout_path: Optional[str] = None) -> Dict[str, Any]:
    """Loads and strictly validates a declarative UI action layout file.

    Structural defects, unsupported versions, malformed actions, non-finite
    or out-of-range normalized coordinates, and unknown fields all fail
    closed by raising UiLayoutError. A missing file raises LAYOUT_NOT_FOUND;
    everything else raises LAYOUT_INVALID naming the offending field.
    """
    path = (
        Path(layout_path).expanduser()
        if layout_path
        else DEFAULT_UI_LAYOUT_PATH
    )
    if not path.is_file():
        raise UiLayoutError(
            "LAYOUT_NOT_FOUND", f"UI layout file not found: {path}"
        )

    def _reject_constant(token: str) -> None:
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"UI layout {path} contains the non-JSON constant '{token}'.",
        )

    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_reject_constant
        )
    except UiLayoutError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        raise UiLayoutError(
            "LAYOUT_INVALID", f"UI layout {path} could not be parsed: {e}"
        )

    if not isinstance(raw, dict):
        raise UiLayoutError(
            "LAYOUT_INVALID", f"UI layout {path} must be a JSON object."
        )
    unknown_top = set(raw) - _LAYOUT_TOP_LEVEL_FIELDS
    if unknown_top:
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"UI layout {path} has unsupported top-level field(s): "
            f"{sorted(unknown_top)}.",
        )
    if raw.get("schema") != UI_LAYOUT_SCHEMA:
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"UI layout {path} field 'schema' must be "
            f"'{UI_LAYOUT_SCHEMA}'.",
        )
    version = raw.get("schemaVersion")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != UI_LAYOUT_SCHEMA_VERSION
    ):
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"UI layout {path} has unsupported schemaVersion {version!r}; "
            f"expected {UI_LAYOUT_SCHEMA_VERSION}.",
        )
    actions = raw.get("actions")
    if not isinstance(actions, dict):
        raise UiLayoutError(
            "LAYOUT_INVALID",
            f"UI layout {path} field 'actions' must be a JSON object.",
        )
    for action_name, action_entry in actions.items():
        if not isinstance(action_name, str) or not action_name.strip():
            raise UiLayoutError(
                "LAYOUT_INVALID",
                f"UI layout {path} contains an action with an empty name.",
            )
        _validate_action_entry(action_name, action_entry, str(path))
    return raw


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

        # Measure the client rectangle in physical device pixels. This CLI is
        # DPI-unaware, so the client_rect above is virtualized (e.g. 1536x960
        # at 125% scaling) while ImageGrab interprets bbox in physical pixels
        # (1920x1200). Feeding the virtual rect produces a literal top-left
        # crop of the frame; the physical rect is required for full coverage.
        try:
            phys = self.driver.get_physical_client_rect(game_hwnd)
        except PhysicalRectUnavailableError as e:
            return {
                "success": False,
                "error": "PHYSICAL_RECT_UNAVAILABLE",
                "message": str(e),
                "target_hwnd": game_hwnd,
            }
        except Exception as e:
            return {
                "success": False,
                "error": "PHYSICAL_RECT_UNAVAILABLE",
                "message": f"Physical client rect measurement failed: {e}",
                "target_hwnd": game_hwnd,
            }

        if phys["width"] <= 0 or phys["height"] <= 0:
            return {
                "success": False,
                "error": "INVALID_CLIENT_DIMENSIONS",
                "message": (
                    f"Game window physical client dimensions are non-positive: "
                    f"{phys['width']}x{phys['height']}."
                ),
                "target_hwnd": game_hwnd,
            }

        vs = phys["virtual_screen"]
        if (
            phys["left"] < vs["left"]
            or phys["top"] < vs["top"]
            or phys["right"] > vs["right"]
            or phys["bottom"] > vs["bottom"]
        ):
            return {
                "success": False,
                "error": "CLIENT_RECT_OFFSCREEN",
                "message": (
                    f"Physical client rect ({phys['left']},{phys['top']},"
                    f"{phys['right']},{phys['bottom']}) is not fully contained "
                    f"in the physical virtual screen ({vs['left']},{vs['top']},"
                    f"{vs['right']},{vs['bottom']}); capture would be clipped."
                ),
                "client_rect_physical": phys,
                "target_hwnd": game_hwnd,
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
            phys["left"],
            phys["top"],
            phys["right"],
            phys["bottom"],
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

        # Fail closed if the returned image does not cover the full physical
        # client rect; never write a partial or mis-sized PNG.
        img_size = getattr(img, "size", None)
        if not img_size or tuple(img_size) != (phys["width"], phys["height"]):
            return {
                "success": False,
                "error": "CAPTURE_SIZE_MISMATCH",
                "message": (
                    f"Captured image size {tuple(img_size) if img_size else 'unknown'} "
                    f"does not match physical client size "
                    f"({phys['width']}, {phys['height']}). Capture rejected."
                ),
                "expected_size": {
                    "width": phys["width"],
                    "height": phys["height"],
                },
                "actual_size": (
                    {"width": img_size[0], "height": img_size[1]}
                    if img_size
                    else None
                ),
                "client_rect_physical": phys,
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
            "client_rect_virtual": c_rect,
            "client_rect_physical": phys,
            "image_size": {"width": img_size[0], "height": img_size[1]},
            "dpi": phys["dpi"] or None,
            "coordinate_space": "device",
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
        return self._guarded_click(self.discover(), x, y)

    def _guarded_click(
        self, data: Dict[str, Any], x: int, y: int
    ) -> Dict[str, Any]:
        """Runs the guarded click flow against a single discovery snapshot.

        `click` passes a fresh `discover()` result; `action_invoke` passes
        the same snapshot it resolved and bounds-checked its anchor against,
        avoiding a stale offset caused by a second discovery. As with direct
        clicks, an OS window change after discovery remains a race limitation.
        """
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

    @staticmethod
    def _layout_path_label(layout_path: Optional[str]) -> str:
        if layout_path:
            return str(Path(layout_path).expanduser())
        return str(DEFAULT_UI_LAYOUT_PATH)

    def action_list(
        self,
        include_provisional: bool = False,
        layout_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Lists declarative UI actions from the layout file.

        Read-only: never enumerates or touches the game window. Provisional
        (uncalibrated) entries are hidden unless include_provisional is set.
        """
        try:
            layout = load_ui_layout(layout_path)
        except UiLayoutError as e:
            return {
                "success": False,
                "command": "action list",
                "error": e.code,
                "message": e.message,
            }

        actions = layout["actions"]
        listed = []
        for name in sorted(actions):
            entry = actions[name]
            if entry["status"] == "provisional" and not include_provisional:
                continue
            listed.append(
                {
                    "name": name,
                    "status": entry["status"],
                    "description": entry["description"],
                    "clientControl": entry["clientControl"],
                    "anchor": entry["anchor"],
                    "anchor_set": entry["anchor"] is not None,
                }
            )

        return {
            "success": True,
            "command": "action list",
            "layout_path": self._layout_path_label(layout_path),
            "schema": layout["schema"],
            "schema_version": layout["schemaVersion"],
            "include_provisional": include_provisional,
            "actions": listed,
            "counts": {
                "listed": len(listed),
                "total": len(actions),
                "calibrated": sum(
                    1 for e in actions.values() if e["status"] == "calibrated"
                ),
                "provisional": sum(
                    1 for e in actions.values() if e["status"] == "provisional"
                ),
            },
        }

    def action_show(
        self, name: str, layout_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """Shows one layout action entry verbatim. Read-only."""
        try:
            layout = load_ui_layout(layout_path)
        except UiLayoutError as e:
            return {
                "success": False,
                "command": "action show",
                "action": name,
                "error": e.code,
                "message": e.message,
            }

        entry = layout["actions"].get(name)
        if entry is None:
            return {
                "success": False,
                "command": "action show",
                "action": name,
                "error": "ACTION_NOT_FOUND",
                "message": f"Action '{name}' is not defined in the layout.",
                "available_actions": sorted(layout["actions"]),
            }

        return {
            "success": True,
            "command": "action show",
            "action": name,
            "layout_path": self._layout_path_label(layout_path),
            "entry": dict(entry),
            "anchor_set": entry["anchor"] is not None,
            "provisional_opt_in_required": entry["status"] == "provisional",
        }

    def action_invoke(
        self,
        name: str,
        allow_provisional: bool = False,
        layout_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolves a named layout anchor and dispatches one guarded click.

        Exactly one click is dispatched per invocation; there is no retry and
        no sequencing. A successful dispatch only proves SendInput accepted
        the event against a verified-foreground window — the named UI effect
        is always reported as verification UNKNOWN.
        """
        try:
            layout = load_ui_layout(layout_path)
        except UiLayoutError as e:
            return {
                "success": False,
                "command": "action invoke",
                "action": name,
                "error": e.code,
                "message": e.message,
            }

        entry = layout["actions"].get(name)
        if entry is None:
            return {
                "success": False,
                "command": "action invoke",
                "action": name,
                "error": "ACTION_NOT_FOUND",
                "message": f"Action '{name}' is not defined in the layout.",
                "available_actions": sorted(layout["actions"]),
            }

        warnings: List[str] = []
        if entry["status"] == "provisional":
            if not allow_provisional:
                return {
                    "success": False,
                    "command": "action invoke",
                    "action": name,
                    "action_status": "provisional",
                    "error": "ACTION_PROVISIONAL",
                    "message": (
                        f"Action '{name}' is provisional (uncalibrated). "
                        "Dispatch is refused unless --allow-provisional is "
                        "passed explicitly."
                    ),
                }
            warnings.append(
                f"Action '{name}' is PROVISIONAL: its anchor is uncalibrated "
                "and dispatch is permitted only because --allow-provisional "
                "was passed. The click may not hit the intended UI control."
            )

        anchor = entry["anchor"]
        if anchor is None:
            return {
                "success": False,
                "command": "action invoke",
                "action": name,
                "action_status": entry["status"],
                "anchor": None,
                "error": "ACTION_ANCHOR_UNSET",
                "message": (
                    f"Action '{name}' has no anchor coordinates; nothing was "
                    "dispatched. A calibration pass must measure the anchor "
                    "first."
                ),
                "warnings": warnings,
            }

        data = self.discover()
        if not data["process"]["found"]:
            return {
                "success": False,
                "command": "action invoke",
                "action": name,
                "action_status": entry["status"],
                "error": "PROCESS_NOT_FOUND",
                "message": "PGR.exe process was not found.",
                "warnings": warnings,
            }

        game_win = data["windows"]["game"]
        if not game_win:
            return {
                "success": False,
                "command": "action invoke",
                "action": name,
                "action_status": entry["status"],
                "error": "GAME_WINDOW_NOT_FOUND",
                "message": "UnityWndClass game window was not found.",
                "warnings": warnings,
            }

        c_rect = game_win["client_rect"]
        c_w = c_rect["width"]
        c_h = c_rect["height"]
        if c_w <= 0 or c_h <= 0:
            return {
                "success": False,
                "command": "action invoke",
                "action": name,
                "action_status": entry["status"],
                "error": "INVALID_CLIENT_DIMENSIONS",
                "message": (
                    f"Game window client dimensions are non-positive: "
                    f"{c_w}x{c_h}; the anchor cannot be resolved."
                ),
                "warnings": warnings,
            }

        # Normalized anchors resolve against the same virtualized client rect
        # that `click` consumes (round(x*client_w), round(y*client_h)).
        resolved_x = int(round(anchor["x"] * c_w))
        resolved_y = int(round(anchor["y"] * c_h))
        resolved_client = {
            "space": "virtual",
            "x": resolved_x,
            "y": resolved_y,
        }
        normalized_anchor = {
            "space": "normalized",
            "x": anchor["x"],
            "y": anchor["y"],
        }

        if not (0 <= resolved_x < c_w and 0 <= resolved_y < c_h):
            return {
                "success": False,
                "command": "action invoke",
                "action": name,
                "action_status": entry["status"],
                "anchor": normalized_anchor,
                "resolved_client": resolved_client,
                "error": "ANCHOR_OUT_OF_BOUNDS",
                "message": (
                    f"Action '{name}' anchor ({anchor['x']}, {anchor['y']}) "
                    f"resolves to ({resolved_x}, {resolved_y}), outside the "
                    f"current client bounds (0, 0, {c_w}, {c_h}). Nothing "
                    "was dispatched."
                ),
                "client_width": c_w,
                "client_height": c_h,
                "warnings": warnings,
            }

        # Dispatch on the SAME discovery snapshot used to resolve and
        # bounds-check the anchor above.
        click_result = self._guarded_click(data, resolved_x, resolved_y)
        # The guarded click only reaches the coordinate-reporting payload
        # after SendInput was attempted; pre-dispatch guard failures return
        # early without it.
        dispatched = "screen_coords" in click_result

        result: Dict[str, Any] = {
            "success": bool(click_result.get("success")),
            "command": "action invoke",
            "action": name,
            "action_status": entry["status"],
            "anchor": normalized_anchor,
            "resolved_client": resolved_client,
            "dispatch": {"type": "click", "dispatched": dispatched},
            "verification": {
                "kind": entry["verification"]["kind"],
                "performed": False,
                "result": "UNKNOWN",
                "message": (
                    "No verification was performed; a dispatched click does "
                    "not prove the named UI effect occurred."
                ),
            },
            "warnings": warnings,
            "click": click_result,
        }
        if click_result.get("error"):
            result["error"] = click_result["error"]
            result["message"] = click_result.get("message")
        for key in (
            "foreground_retained",
            "foreground_after",
            "target_hwnd",
            "race_limitation_warning",
        ):
            if key in click_result:
                result[key] = click_result[key]
        return result


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

    # action (declarative named UI actions from the layout file)
    action_parser = subparsers.add_parser(
        "action",
        help="List, inspect, or invoke named UI actions from the layout file",
    )
    action_subparsers = action_parser.add_subparsers(
        dest="action_command", required=True
    )

    action_list_parser = action_subparsers.add_parser(
        "list",
        help="List layout actions (calibrated only unless --include-provisional)",
    )
    action_list_parser.add_argument(
        "--include-provisional",
        action="store_true",
        help="Also list provisional (uncalibrated) actions",
    )
    action_list_parser.add_argument(
        "--layout",
        default=None,
        help="Override layout file path (validated with the same rules)",
    )

    action_show_parser = action_subparsers.add_parser(
        "show", help="Show one layout action entry"
    )
    action_show_parser.add_argument(
        "name", help="Action name (e.g. lobby.enter)"
    )
    action_show_parser.add_argument(
        "--layout",
        default=None,
        help="Override layout file path (validated with the same rules)",
    )

    action_invoke_parser = action_subparsers.add_parser(
        "invoke",
        help="Resolve a named anchor and dispatch exactly one guarded click",
    )
    action_invoke_parser.add_argument(
        "name", help="Action name (e.g. lobby.enter)"
    )
    action_invoke_parser.add_argument(
        "--allow-provisional",
        action="store_true",
        help="Allow dispatch of provisional (uncalibrated) actions",
    )
    action_invoke_parser.add_argument(
        "--layout",
        default=None,
        help="Override layout file path (validated with the same rules)",
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
    elif args.command == "action":
        if args.action_command == "list":
            result = controller.action_list(
                include_provisional=args.include_provisional,
                layout_path=args.layout,
            )
        elif args.action_command == "show":
            result = controller.action_show(
                name=args.name,
                layout_path=args.layout,
            )
        elif args.action_command == "invoke":
            result = controller.action_invoke(
                name=args.name,
                allow_provisional=args.allow_provisional,
                layout_path=args.layout,
            )
        else:
            result = {
                "success": False,
                "error": "UNKNOWN_COMMAND",
                "message": f"Unknown action subcommand: {args.action_command}",
            }
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
