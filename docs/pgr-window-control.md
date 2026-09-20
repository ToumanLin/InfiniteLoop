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
   - *Mitigation:* The CLI verifies that the game client is in the foreground and visible immediately before capture, measures the exact client rectangle in physical device pixels (see §3.3), captures the frame, asserts the returned image covers the full rectangle, and re-verifies foreground state immediately after. If foreground changed during capture, the operation fails and the capture is rejected.

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
Captures the visible client area of `PGR.exe` as a PNG file in **physical device pixels**.

```bash
python Scripts/pgr_window_control.py screenshot [--out .runtime/screenshots/my_capture.png] [--allow-krsdk-foreground]
```

**Physical-frame contract (DPI correctness):**
- This CLI is intentionally **DPI-unaware**, so `GetClientRect`/`ClientToScreen`/`GetSystemMetrics` return *virtualized* coordinates (e.g. `1536x960` on a 125%-scaled 1920x1200 display). `PIL.ImageGrab.grab(bbox=...)`, however, interprets `bbox` in **physical device pixels**. Feeding the virtualized rect to `ImageGrab` previously produced a literal top-left `1536x960` crop of the `1920x1200` frame, discarding the right and bottom 20% of every capture.
- `screenshot` therefore measures the client rectangle via `Win32Driver.get_physical_client_rect()`: the calling thread temporarily adopts `DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2` (falling back to `PER_MONITOR_AWARE` — both yield physical coordinates; `SYSTEM_AWARE` is *not* used because it can still rescale under mixed-DPI multi-monitor layouts), reads `GetClientRect`/`ClientToScreen`/virtual-screen metrics/`GetDpiForWindow`, and **always restores the prior thread context** afterwards, including on failure. Process DPI awareness is never changed globally, and no fixed scale factor is assumed.
- The measured physical rect must have positive dimensions and lie **fully inside the physical virtual screen** (`SM_*VIRTUALSCREEN` read under the same aware context); otherwise the command fails closed (`INVALID_CLIENT_DIMENSIONS` / `CLIENT_RECT_OFFSCREEN`) rather than writing a clipped PNG.
- After capture, `img.size` must equal the physical client size exactly; any deviation fails with `CAPTURE_SIZE_MISMATCH` and nothing is written.

**Contract & Safety Guards:**
- Requires `Pillow` (installed optional dependency; fails clearly with `DEPENDENCY_MISSING` if absent).
- Requires `game` window (or KRSDK modal if `--allow-krsdk-foreground` is set) to be in the foreground *before* capture.
- Verifies window visibility and non-minimized state.
- Fails with `PHYSICAL_RECT_UNAVAILABLE` if a reliable physical measurement cannot be obtained (missing or rejected DPI-awareness API, measurement error).
- Verifies foreground *immediately after* capture. If focus changed during capture, returns `FOREGROUND_RACE_POST` and rejects the capture.
- Default output directory is `.runtime/screenshots/`, which is ignored by version control.

**Success JSON (additive fields):**
```json
{
  "success": true,
  "output_path": "...",
  "target_hwnd": 5770964,
  "client_rect": {"left": 0, "top": 0, "right": 1536, "bottom": 960, "width": 1536, "height": 960},
  "client_rect_virtual": {"left": 0, "top": 0, "right": 1536, "bottom": 960, "width": 1536, "height": 960},
  "client_rect_physical": {"left": 0, "top": 0, "right": 1920, "bottom": 1200, "width": 1920, "height": 1200, "dpi": 120, "dpi_awareness_context": "PER_MONITOR_AWARE_V2", "virtual_screen": {"left": 0, "top": 0, "right": 1920, "bottom": 1200, "width": 1920, "height": 1200}},
  "image_size": {"width": 1920, "height": 1200},
  "dpi": 120,
  "coordinate_space": "device",
  "race_limitation_warning": "..."
}
```
`client_rect`/`client_rect_virtual` remain the legacy *virtualized* rect (back-compat; it is what `click` coordinates address). `client_rect_physical`, `image_size`, `dpi`, and `coordinate_space="device"` describe the captured pixels. The capture is **not** guaranteed occlusion-free: a topmost window overlapping the client area would still be sampled; the race/occlusion limitation in §2 applies unchanged.

**New error codes:** `PHYSICAL_RECT_UNAVAILABLE` (reliable physical measurement impossible), `CLIENT_RECT_OFFSCREEN` (physical client rect not fully on-screen), `CAPTURE_SIZE_MISMATCH` (returned image size != physical client size; no file written).

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
- Coordinates are in the **virtualized** client space (`client_rect`/`client_rect_virtual` from `status`/`screenshot`, e.g. `1536x960` at 125% scaling) — unchanged legacy semantics. `SendInput` absolute+virtualdesk normalization maps virtual screen points proportionally onto physical pixels, so legacy coordinates remain self-consistent even though `screenshot` captures in device pixels.
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

### 3.7 `action`
Lists, inspects, or invokes **declarative named UI actions** defined in the tracked layout file `Scripts/pgr_ui_layout.json`. This is phase-1 accessibility groundwork: the Unity client exposes no Win32/UIA/MSAA control tree, so named actions are the CLI's semantic vocabulary over guarded pixel dispatch.

```bash
python Scripts/pgr_window_control.py action list [--include-provisional] [--layout PATH]
python Scripts/pgr_window_control.py action show <name> [--layout PATH]
python Scripts/pgr_window_control.py action invoke <name> [--allow-provisional] [--layout PATH]
```

**Layout file (`Scripts/pgr_ui_layout.json`, schema `pgr-ui-layout` v1):**
Each action entry carries `description`, `clientControl` (client-side control provenance, e.g. the Lua name), `anchor`, `status`, `dispatch`, and `verification`; `calibrationSource` is required on `calibrated` entries. Anchors are `{"type": "normalized_point", "x", "y"}` with finite coordinates in `0 <= v < 1` — resolution-independent fractions of the client area. `invoke` resolves an anchor to **virtual** client pixels (`round(x*client_w)`, `round(y*client_h)` — the same virtualized coordinate space `click` uses) and delegates to the existing guarded `click` path.

The loader is strict and fails closed: a missing file is `LAYOUT_NOT_FOUND`; wrong `schema`/`schemaVersion`, malformed JSON (including `NaN`/`Infinity` constants), unknown fields, missing required fields, non-`click` dispatch types, and out-of-range or non-finite coordinates are all `LAYOUT_INVALID` naming the offending field. `verification` objects follow a per-kind key allowlist: `kind: "none"` takes no extra keys, and `kind: "screenshot-diff"` may carry an optional finite numeric `minChangeRatio` in `(0, 1]`; unknown keys or malformed ratios are `LAYOUT_INVALID`.

**Action status vocabulary:**
- `calibrated` — the anchor was measured against a real client frame and the entry names its `calibrationSource` evidence. Dispatchable as-is.
- `provisional` — the control is named (client Lua vocabulary) but its anchor is unmeasured. `action list` hides these unless `--include-provisional` is passed; `action invoke` refuses them with `ACTION_PROVISIONAL` unless `--allow-provisional` is passed explicitly, and always emits a warning when it does dispatch one. A provisional entry with `anchor: null` fails closed with `ACTION_ANCHOR_UNSET` even under `--allow-provisional` — no invented coordinates are ever dispatched.

**`action invoke` flow:** load+validate layout → `ACTION_NOT_FOUND` if absent → `ACTION_PROVISIONAL` gate → `ACTION_ANCHOR_UNSET` gate → fresh `discover()` → resolve anchor → `ANCHOR_OUT_OF_BOUNDS` if the resolved pixel lands outside the *current* client rect → exactly **one** guarded `click` (all of §3.5's foreground checks apply unchanged). No retries, no multi-step sequences, ever.

**Result JSON:**
```json
{
  "success": true,
  "command": "action invoke",
  "action": "lobby.enter",
  "action_status": "calibrated",
  "anchor": {"space": "normalized", "x": 0.5, "y": 0.5},
  "resolved_client": {"space": "virtual", "x": 768, "y": 480},
  "dispatch": {"type": "click", "dispatched": true},
  "verification": {"kind": "none", "performed": false, "result": "UNKNOWN", "message": "..."},
  "warnings": [],
  "foreground_retained": true,
  "race_limitation_warning": "...",
  "click": { "..." : "full inner click result" }
}
```

**Honesty contract:** `verification.result` is always `UNKNOWN` in this slice — a dispatched `SendInput` does **not** prove the named UI control was hit or that the intended effect occurred. `dispatch.dispatched` only means the click reached `SendInput`; downstream guard failures (e.g. `FOREGROUND_MISMATCH_PRE`, `FOREGROUND_LOST_RACE`) propagate with `dispatched: false`. Declared verification kinds like `screenshot-diff` are accepted by the loader as policy but are **not executed** here.

**New error codes:** `LAYOUT_NOT_FOUND`, `LAYOUT_INVALID`, `ACTION_NOT_FOUND`, `ACTION_PROVISIONAL`, `ACTION_ANCHOR_UNSET`, `ANCHOR_OUT_OF_BOUNDS`. Exit codes unchanged: `0` success, `1` guarded failure, `2` argparse usage errors.

**`--layout PATH`:** overrides the default layout (intended for local calibration fixtures under ignored paths). The override is validated with exactly the same rules; there is no relaxed mode.

**Photo-mode calibration (user-measured on a 1920×1200 physical client, 2026-09-20):** The rectangles below are physical-client `Rect(x,y,width,height)`, while `action invoke` still resolves the center to the CLI's virtual client pixels. The bar rectangle is tilted about 6° clockwise; its geometric center is the anchor. Calibration means the position was measured, **not** that the click's semantic effect was verified. Capture and inspect the state after each action.

| Action or region | Physical XYWH | Virtual click at 1536×960 | Status |
|---|---:|---:|---|
| `main_terminal.bottom_bar_toggle` | `(1328,953,514,69)` | `(1268,790)` | Calibrated center |
| `main_terminal.camera_button` | `(1107,900,127,115)` | `(936,766)` | Calibrated center |
| `photograph.btn_hide` (eye) | `(1811,269,84,80)` | `(1482,247)` | Calibrated center |
| `photograph.btn_scene` | `(1811,368,84,87)` | `(1482,329)` | Calibrated center |
| `photograph.scene_list` scroll region | `(242,149,391,925)` | — | Region only; no one-click anchor or scroll command |

Each invocation performs at most one guarded click. For the user-confirmed route, invoke the bottom-bar toggle, verify a screenshot, invoke the camera button, verify the scene list and eye/scene icons, then invoke the eye button and verify that other UI is hidden. `photograph.btn_scene` opens the scene-change control; **which choice selects day or night remains UNKNOWN**. Do not batch these commands or infer an effect from `success:true` alone.

**Route vocabulary & support matrix (as shipped):**

| Status | Actions | Basis |
|---|---|---|
| `calibrated` | `lobby.enter` (0.5, 0.5); `lobby.dismiss_neutral_top` (0.5, 0.208); `lobby.dismiss_left_margin` (0.039, 0.5); `lobby.dismiss_top_left` (0.052, 0.083) | Observed working in the 2026-09-20 activation run (virtual 1536x960 normalized). The `dismiss_*` targets are neutral tap points; which element each dismissed is unverified. |
| `calibrated` (user-measured anchor) | `main_terminal.bottom_bar_toggle`, `main_terminal.camera_button`, `photograph.btn_hide`, `photograph.btn_scene` | User-supplied physical rectangles above; normalized centers stored in `pgr_ui_layout.json`. Effects must still be checked by screenshot. |
| `provisional` (anchor unset) | `photograph.scene_change_1..3` (`XUiPanelPhotographSceneChange.BtnSceneChange1/2/3`), `photograph.scene_list` (`XUiSceneSettingMain`), `scene_setting.open` (`OpenUiSceneSetting`) | Day/night mapping and these click targets remain unverified. The scene-list rectangle is a measured scroll **region**, not a calibrated click. |

**Explicit limitation:** the four photo-route anchors above are based on the user's measurements, not a CLI-verified state transition. `scene_change_1..3`, the scene-list scroll interaction, and `scene_setting.open` remain gated/unset. Nothing in this surface reads in-process UI state or confirms effects; `UNKNOWN` is never promoted to a success claim.

---

## 4. Testing & Verification

Run the focused unit test suite:
```bash
python -m unittest test_pgr_window_control.py
```

The test suite uses `FakeWin32Driver` to simulate all Win32 GUI states, ownership trees, foreground races, boundary violations, DPI virtualization, control IDs, and auth results without interacting with any live desktop processes.

**Deferred live verification (requires an unlocked console):** the physical-capture path is unit-tested only against `FakeWin32Driver`. Once the workstation is unlocked and the game window can hold foreground, run `python Scripts/pgr_window_control.py screenshot` against the live client and confirm the emitted `client_rect_physical`/`image_size` equal `1920x1200` (or the display's native resolution) and that the PNG shows the full frame rather than a top-left crop. Note that while the console is locked (`LockApp` foreground) the command correctly fails with `FOREGROUND_MISMATCH_PRE`.
