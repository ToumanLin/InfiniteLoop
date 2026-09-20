# PGR Windows Control CLI (`Scripts/pgr_window_control.py`)

External Windows automation and diagnostic CLI for Punishing: Gray Raven (`PGR.exe`), designed for unattended agent operations and native render capture workflows.

---

## 1. Overview & Architecture

When running local native client automation with `AscNet` and `KRSDK`, the game client opens Win32 GUI windows (`UnityWndClass` and `KRSDK_MainWindow` / `KRSDK_Login`) that require focus management, authentication dialog interaction, and pointer/keyboard input.

`pgr_window_control.py` is an external automation utility that executes directly on Windows via `ctypes` (`user32` and `kernel32`). It operates under a **guarded best-effort** contract:
- Every invocation freshly resolves the `PGR.exe` process identity, top-level `UnityWndClass` window, and same-PID owned KRSDK windows. It **never** caches or reuses raw HWND or PID values across runs.
- Window hierarchy and PID ownership are verified at every step to prevent control hijacking or accidental interaction with unrelated applications.
- Foreground state and coordinate bounds are strictly verified before and after input or capture operations.
- All commands emit structured JSON to `stdout` with standardized status and error payloads.

---

## 2. Guarded Best-Effort Routing & Win32 Limitations

Under the Windows Win32 User subsystem, certain operations cannot provide mathematical atomicity:
1. **SendInput Dispatches to Global OS Queue:**
   `SendInput` does not accept a destination `HWND`; it injects keyboard and mouse events into the OS-wide desktop input stream. If another application or notification gains focus in the microsecond between a foreground check and event dispatch, the input will be routed to the new foreground window.
   - *Mitigation:* The CLI performs a strict `GetForegroundWindow()` verification immediately prior to `SendInput`, verifies coordinate bounds against `GetClientRect`, and performs a post-dispatch foreground verification. If the foreground window changes, the failure is reported immediately. The CLI **never performs an implicit click or key retry**.
2. **Desktop Pixel Capture & Occlusion Races:**
   Win32 screen capture (`PIL.ImageGrab`) captures the visible screen raster within the specified bounding box. While window identity and dimensions are verified, Win32 does not provide atomic isolation against transparent topmost windows or rapid focus races between before/after checks.
   - *Mitigation:* The CLI verifies that the game client is in the foreground and visible immediately before capture, computes the exact client rectangle via `ClientToScreen`, captures the frame, and re-verifies foreground state immediately after. If foreground changed during capture, the operation fails and the capture is rejected.

---

## 3. Command Reference

### 3.1 `status` (or `list`)
Inspects and lists running `PGR.exe` process details, associated windows, dimensions, and foreground alignment.

```bash
python Scripts/pgr_window_control.py status
```

**Output Schema:**
```json
{
  "process": {
    "pid": 20600,
    "name": "PGR.exe",
    "path": "C:\\Program Files (x86)\\Steam\\steamapps\\common\\Punishing Gray Raven\\PGR.exe",
    "found": true
  },
  "windows": {
    "game": {
      "hwnd": 5770964,
      "pid": 20600,
      "path": "...",
      "class_name": "UnityWndClass",
      "title": "PGR",
      "owner_hwnd": 0,
      "is_visible": true,
      "is_iconic": false,
      "window_rect": {"left": 0, "top": 0, "right": 1536, "bottom": 960, "width": 1536, "height": 960},
      "client_rect": {"left": 0, "top": 0, "right": 1536, "bottom": 960, "width": 1536, "height": 960}
    },
    "krsdk_main": {
      "hwnd": 19596018,
      "class_name": "KRSDK_MainWindow",
      "title": "KRSDK",
      "owner_hwnd": 5770964,
      ...
    },
    "krsdk_login": null,
    "message_box": null
  },
  "foreground": {
    "hwnd": 8588532,
    "pid": 18168,
    "matches_game": false,
    "matches_krsdk": false
  }
}
```

---

### 3.2 `focus`
Brings the target window to the foreground using multi-tier Win32 foreground activation, verifying the result with a timeout loop.

```bash
python Scripts/pgr_window_control.py focus [--target game|krsdk_main|krsdk_login] [--timeout 3.0]
```

**Activation Sequence:**
1. Restores window with `ShowWindow(hwnd, SW_RESTORE)` if minimized.
2. Attaches caller thread input to the foreground window thread via `AttachThreadInput`.
3. Calls `AllowSetForegroundWindow(ASFW_ANY)`.
4. Calls `SwitchToThisWindow(hwnd, TRUE)`, `SetForegroundWindow`, `BringWindowToTop`, and `SetActiveWindow`.
5. Detaches thread input.
6. Polls `GetForegroundWindow()` every 50ms up to `--timeout` seconds to verify foreground match.

---

### 3.3 `screenshot`
Captures the visible client area of `PGR.exe` as a PNG file.

```bash
python Scripts/pgr_window_control.py screenshot [--out .runtime/screenshots/my_capture.png] [--allow-krsdk-foreground]
```

**Contract & Safety Guards:**
- Requires `Pillow` (installed optional dependency; fails clearly with `DEPENDENCY_MISSING` if absent).
- Requires `game` window (or KRSDK modal if `--allow-krsdk-foreground` is set) to be in the foreground *before* capture.
- Verifies window visibility and non-minimized state.
- Computes screen client coordinates using `ClientToScreen`.
- Verifies foreground *immediately after* capture. If focus changed during capture, returns `FOREGROUND_RACE_POST` and rejects the capture.
- Default output directory is `.runtime/screenshots/`, which is ignored by version control.

---

### 3.4 `login`
Automates the KRSDK Win32 login modal sequence.

```bash
# PowerShell: credentials MUST come from environment variables.
$env:ASCNET_USERNAME = "test"
$env:ASCNET_PASSWORD = "123"

python Scripts/pgr_window_control.py login [--timeout 10.0]
```

**Interaction Flow & Control IDs:**
1. Validates that `ASCNET_USERNAME` and `ASCNET_PASSWORD` are present and non-empty.
2. **Privacy Guarantee:** The password is **never** printed to stdout, stderr, logs, or JSON payloads.
3. If `KRSDK_Login` dialog is not open, locates `ID_BTN_LOGIN` (`10001`) on `KRSDK_MainWindow`. Verifies control PID matches `PGR.exe`, then posts `BM_CLICK`.
4. Waits for `KRSDK_Login` to open. Verifies that it belongs to the same-PID ownership chain rooted at the PGR game window.
5. Populates:
   - Username Edit: `ID_EDIT_USERNAME` (`1001`) via `WM_SETTEXT`.
   - Password Edit: `ID_EDIT_PASSWORD` (`1002`) via `WM_SETTEXT`.
6. Dispatches submit on Button `ID_BTN_SUBMIT` (`1`) via `PostMessageW(..., BM_CLICK, ...)`.
   *(Using `PostMessageW` prevents the caller from deadlocking on modal Win32 message boxes.)*
7. Observes result:
   - If `#32770` dialog with title `"Success"` appears: extracts the message, synchronously clicks its actual `OK` child control (KRSDK has used dialog control ID `1` or `2`), requires the dialog and KRSDK windows to close, then returns `LOGIN_SUCCESS`.
   - If `#32770` dialog with title `"Error"` appears: extracts message, dismisses dialog, and returns `LOGIN_FAILED`.
   - If windows close cleanly: returns `LOGIN_SUCCESS`.
   - If `--timeout` expires: returns `TIMEOUT_UNCERTAIN` and explicitly confirms **submission was NOT retried**.
   - If a result dialog already exists when the command starts, returns `PENDING_DIALOG` without another submission.

---

### 3.5 `click`
Dispatches a mouse click at game-client-relative coordinates `(x, y)`.

```bash
python Scripts/pgr_window_control.py click --x 768 --y 480
```

**Contract & Safety Guards:**
- Validates bounds: `0 <= x < client_width` and `0 <= y < client_height`. Rejects out-of-bounds coordinates with `OUT_OF_BOUNDS`.
- Verifies game window is in foreground before conversion.
- Converts client-relative coordinates to absolute virtual screen coordinates.
- Checks foreground immediately before `SendInput`.
- Checks foreground immediately after `SendInput`.
- Reports `foreground_retained` in JSON output. No implicit retry.

---

### 3.6 `key`
Dispatches a keyboard event to the game window.

```bash
python Scripts/pgr_window_control.py key --key space
```

**Supported Keys:**
`space`, `enter`, `return`, `escape`, `esc`, `tab`, `backspace`, `up`, `down`, `left`, `right`, `a-z`, `0-9`, and `f1-f24` (including RenderDoc's default `f12` capture key).

**Contract & Safety Guards:**
- Verifies game window is in foreground before input.
- Checks foreground immediately before `SendInput`.
- Dispatches key down and key up via `SendInput`.
- Checks foreground immediately after `SendInput`. No implicit retry.

---

## 4. Testing & Verification

Run the focused unit test suite:
```bash
python -m unittest test_pgr_window_control.py
```

The test suite uses `FakeWin32Driver` to simulate all Win32 GUI states, ownership trees, foreground races, boundary violations, control IDs, and auth results without interacting with any live desktop processes.
