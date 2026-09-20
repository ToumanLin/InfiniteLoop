#!/usr/bin/env python3
"""Focused unit test suite for external Windows PGR window control CLI."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from Scripts.pgr_window_control import (
    DEFAULT_UI_LAYOUT_PATH,
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    DPI_AWARENESS_CONTEXT_UNAWARE,
    FakeWin32Driver,
    FakeWindow,
    PgrWindowController,
    build_parser,
    load_ui_layout,
    main,
    ID_BTN_LOGIN,
    ID_BTN_SUBMIT,
    ID_EDIT_USERNAME,
    ID_EDIT_PASSWORD,
)


def create_standard_test_environment() -> Tuple[FakeWin32Driver, PgrWindowController]:
    """Sets up a mock Windows desktop with PGR.exe, UnityWndClass, and KRSDK."""
    driver = FakeWin32Driver()
    pgr_pid = 20600
    pgr_path = r"C:\Program Files (x86)\Steam\steamapps\common\Punishing Gray Raven\PGR.exe"

    driver.processes[pgr_pid] = pgr_path
    driver.processes[1234] = r"C:\Windows\explorer.exe"

    # Game Window. Models the live 125%-DPI machine: the DPI-unaware CLI sees
    # a virtualized 1536x960 client, while the physical client is 1920x1200.
    game_win = FakeWindow(
        hwnd=5770964,
        pid=pgr_pid,
        class_name="UnityWndClass",
        title="PGR",
        owner_hwnd=0,
        is_visible=True,
        is_iconic=False,
        window_rect=(0, 0, 1536, 960),
        client_rect=(0, 0, 1536, 960),
        client_origin=(0, 0),
        physical_client_rect=(0, 0, 1920, 1200),
        physical_client_origin=(0, 0),
        dpi=120,
    )

    # KRSDK Main Window
    krsdk_win = FakeWindow(
        hwnd=19596018,
        pid=pgr_pid,
        class_name="KRSDK_MainWindow",
        title="KRSDK",
        owner_hwnd=5770964,
        is_visible=True,
        is_iconic=False,
        window_rect=(608, 360, 928, 600),
        client_rect=(0, 0, 305, 202),
        client_origin=(615, 390),
    )
    # Child button 10001 (Login)
    btn_login = FakeWindow(
        hwnd=10001,
        pid=pgr_pid,
        class_name="Button",
        title="Login",
        owner_hwnd=19596018,
    )
    krsdk_win.children[ID_BTN_LOGIN] = 10001

    driver.windows[5770964] = game_win
    driver.windows[19596018] = krsdk_win
    driver.windows[10001] = btn_login

    # Initially foreground is an unrelated window
    driver.foreground_hwnd = 99999
    driver.windows[99999] = FakeWindow(
        hwnd=99999,
        pid=1234,
        class_name="CabinetWClass",
        title="File Explorer",
    )

    controller = PgrWindowController(driver=driver)
    return driver, controller


class TestPgrWindowControl(unittest.TestCase):
    """Test suite verifying safety bounds, ownership, foreground, and KRSDK logic."""

    def test_process_not_found_when_empty(self) -> None:
        driver = FakeWin32Driver()
        controller = PgrWindowController(driver=driver)

        res = controller.status()
        self.assertFalse(res["process"]["found"])
        self.assertIsNone(res["process"]["pid"])
        self.assertIsNone(res["windows"]["game"])

        focus_res = controller.focus()
        self.assertFalse(focus_res["success"])
        self.assertEqual(focus_res["error"], "PROCESS_NOT_FOUND")

        click_res = controller.click(10, 10)
        self.assertFalse(click_res["success"])
        self.assertEqual(click_res["error"], "PROCESS_NOT_FOUND")

    def test_process_filtering_rejects_non_pgr(self) -> None:
        driver = FakeWin32Driver()
        driver.processes[5555] = r"C:\Games\OtherGame\OtherGame.exe"
        driver.windows[111] = FakeWindow(
            hwnd=111, pid=5555, class_name="UnityWndClass", title="OtherGame"
        )
        controller = PgrWindowController(driver=driver)

        res = controller.status()
        self.assertFalse(res["process"]["found"])
        self.assertIsNone(res["windows"]["game"])

    def test_window_hierarchy_discovery(self) -> None:
        driver, controller = create_standard_test_environment()
        status = controller.status()

        self.assertTrue(status["process"]["found"])
        self.assertEqual(status["process"]["pid"], 20600)
        self.assertIsNotNone(status["windows"]["game"])
        self.assertEqual(status["windows"]["game"]["hwnd"], 5770964)
        self.assertIsNotNone(status["windows"]["krsdk_main"])
        self.assertEqual(status["windows"]["krsdk_main"]["hwnd"], 19596018)
        self.assertEqual(status["windows"]["krsdk_main"]["owner_hwnd"], 5770964)

        # Foreground is unrelated window
        self.assertFalse(status["foreground"]["matches_game"])
        self.assertFalse(status["foreground"]["matches_krsdk"])

    def test_discovery_rejects_unowned_same_pid_dialogs(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.windows[2100] = FakeWindow(
            hwnd=2100,
            pid=20600,
            class_name="KRSDK_Login",
            title="Unrelated login",
            owner_hwnd=0,
        )
        driver.windows[4100] = FakeWindow(
            hwnd=4100,
            pid=20600,
            class_name="#32770",
            title="Unrelated dialog",
            owner_hwnd=0,
        )

        status = controller.status()
        self.assertIsNone(status["windows"]["krsdk_login"])
        self.assertIsNone(status["windows"]["message_box"])

    def test_krsdk_login_preserves_password_whitespace(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.auth_simulation = "timeout"
        password = "  exact password  "

        with patch.dict(
            os.environ,
            {"ASCNET_USERNAME": "testuser", "ASCNET_PASSWORD": password},
        ):
            controller.login_krsdk(timeout=0.01, poll_interval=0.001)

        password_sets = [
            action
            for action in driver.recorded_actions
            if action.get("action") == "set_text" and action.get("hwnd") == 3002
        ]
        self.assertEqual(password_sets, [{"action": "set_text", "hwnd": 3002, "length": len(password)}])

    def test_focus_activation_and_verification(self) -> None:
        driver, controller = create_standard_test_environment()

        # Focus game window
        res = controller.focus(target="game", timeout=1.0)
        self.assertTrue(res["success"])
        self.assertEqual(res["target_hwnd"], 5770964)
        self.assertEqual(driver.foreground_hwnd, 5770964)

        # Test iconic (minimized) restoration
        driver.windows[5770964].is_iconic = True
        res2 = controller.focus(target="game", timeout=1.0)
        self.assertTrue(res2["success"])
        self.assertFalse(driver.windows[5770964].is_iconic)

    def test_screenshot_fails_if_game_not_foreground_pre(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 99999  # Unrelated window

        res = controller.screenshot()
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "FOREGROUND_MISMATCH_PRE")

    def test_screenshot_fails_if_foreground_lost_during_capture(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964  # Game window is foreground
        driver.foreground_race_during_screenshot = True  # Simulates focus race

        res = controller.screenshot()
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "FOREGROUND_RACE_POST")

    def test_screenshot_fails_if_pillow_missing(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964
        driver.pillow_available = False

        res = controller.screenshot()
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "DEPENDENCY_MISSING")
        self.assertIn("Pillow is not installed", res["message"])

    def test_screenshot_success_writes_file_and_includes_race_warning(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964
        driver.pillow_available = True

        out_path = Path(".runtime") / "test_screenshot.png"
        if out_path.exists():
            out_path.unlink()

        res = controller.screenshot(output_path=str(out_path))
        self.assertTrue(res["success"])
        self.assertTrue(out_path.exists())
        self.assertIn("race_limitation_warning", res)
        # Cleanup
        out_path.unlink()

    def test_screenshot_uses_physical_client_bbox_and_reports_fields(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964
        driver.pillow_available = True

        out_path = Path(".runtime") / "test_screenshot_physical.png"
        if out_path.exists():
            out_path.unlink()

        res = controller.screenshot(output_path=str(out_path))
        self.assertTrue(res["success"])

        # The grab bbox must be the physical device-pixel rect (1920x1200),
        # not the DPI-virtualized 1536x960 top-left crop.
        grabs = [
            a for a in driver.recorded_actions if a.get("action") == "grab_screen"
        ]
        self.assertEqual(len(grabs), 1)
        self.assertEqual(grabs[0]["bbox"], (0, 0, 1920, 1200))

        # Additive JSON contract: legacy virtual rect retained, physical rect
        # and device-pixel metadata reported.
        self.assertEqual(res["client_rect"]["width"], 1536)
        self.assertEqual(res["client_rect"]["height"], 960)
        self.assertEqual(res["client_rect_virtual"]["width"], 1536)
        self.assertEqual(res["client_rect_physical"]["width"], 1920)
        self.assertEqual(res["client_rect_physical"]["height"], 1200)
        self.assertEqual(res["image_size"], {"width": 1920, "height": 1200})
        self.assertEqual(res["dpi"], 120)
        self.assertEqual(res["coordinate_space"], "device")

        # The DPI-awareness context must be restored after measurement.
        self.assertEqual(
            driver.thread_dpi_awareness_context, DPI_AWARENESS_CONTEXT_UNAWARE
        )
        ctx_calls = [
            a
            for a in driver.recorded_actions
            if a.get("action") == "set_thread_dpi_awareness_context"
        ]
        self.assertEqual(len(ctx_calls), 2)
        self.assertEqual(
            ctx_calls[0]["context"], DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
        self.assertEqual(
            ctx_calls[1]["context"], DPI_AWARENESS_CONTEXT_UNAWARE
        )

        out_path.unlink()

    def test_screenshot_fails_on_capture_size_mismatch(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964
        driver.pillow_available = True
        # Simulate an under-delivered grab (e.g. clipped raster).
        driver.grabbed_size_override = (1536, 960)

        out_path = Path(".runtime") / "test_screenshot_mismatch.png"
        if out_path.exists():
            out_path.unlink()

        res = controller.screenshot(output_path=str(out_path))
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "CAPTURE_SIZE_MISMATCH")
        self.assertEqual(
            res["expected_size"], {"width": 1920, "height": 1200}
        )
        self.assertEqual(res["actual_size"], {"width": 1536, "height": 960})
        # A partial/mis-sized PNG must never be written.
        self.assertFalse(out_path.exists())

    def test_screenshot_fails_if_physical_rect_offscreen(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964
        driver.pillow_available = True

        # Partially off the left edge of the physical virtual screen.
        driver.windows[5770964].physical_client_origin = (-100, 0)
        res = controller.screenshot()
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "CLIENT_RECT_OFFSCREEN")

        # Partially off the right edge (100 + 1920 > 1920 virtual width).
        driver.windows[5770964].physical_client_origin = (100, 0)
        res = controller.screenshot()
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "CLIENT_RECT_OFFSCREEN")

        # Fully offscreen.
        driver.windows[5770964].physical_client_origin = (5000, 0)
        res = controller.screenshot()
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "CLIENT_RECT_OFFSCREEN")

    def test_screenshot_fails_if_physical_measurement_unsupported(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964
        driver.pillow_available = True
        driver.dpi_context_api_available = False

        res = controller.screenshot()
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "PHYSICAL_RECT_UNAVAILABLE")

        grabs = [
            a for a in driver.recorded_actions if a.get("action") == "grab_screen"
        ]
        self.assertEqual(len(grabs), 0)

    def test_screenshot_restores_dpi_context_on_measurement_error(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964
        driver.pillow_available = True
        driver.raise_during_aware_measurement = True

        res = controller.screenshot()
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "PHYSICAL_RECT_UNAVAILABLE")

        # Prior thread context is restored even when measurement raised.
        self.assertEqual(
            driver.thread_dpi_awareness_context, DPI_AWARENESS_CONTEXT_UNAWARE
        )
        ctx_calls = [
            a
            for a in driver.recorded_actions
            if a.get("action") == "set_thread_dpi_awareness_context"
        ]
        self.assertEqual(len(ctx_calls), 2)
        self.assertEqual(
            ctx_calls[-1]["context"], DPI_AWARENESS_CONTEXT_UNAWARE
        )

    def test_screenshot_fails_if_physical_dimensions_nonpositive(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964
        driver.pillow_available = True
        driver.windows[5770964].physical_client_rect = (0, 0, 0, 0)

        res = controller.screenshot()
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "INVALID_CLIENT_DIMENSIONS")

    def test_click_fails_if_game_not_foreground_pre(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 99999

        res = controller.click(100, 100)
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "FOREGROUND_MISMATCH_PRE")

    def test_click_fails_if_coordinates_out_of_bounds(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964  # Game is foreground

        # Negative coordinates
        res1 = controller.click(-1, 100)
        self.assertFalse(res1["success"])
        self.assertEqual(res1["error"], "OUT_OF_BOUNDS")

        res2 = controller.click(100, -5)
        self.assertFalse(res2["success"])
        self.assertEqual(res2["error"], "OUT_OF_BOUNDS")

        # Equal or exceeding client width (1536) / height (960)
        res3 = controller.click(1536, 500)
        self.assertFalse(res3["success"])
        self.assertEqual(res3["error"], "OUT_OF_BOUNDS")

        res4 = controller.click(500, 960)
        self.assertFalse(res4["success"])
        self.assertEqual(res4["error"], "OUT_OF_BOUNDS")

    def test_click_success_dispatches_without_retry(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        res = controller.click(500, 300)
        self.assertTrue(res["success"])
        self.assertEqual(res["client_coords"], {"x": 500, "y": 300})
        self.assertTrue(res["foreground_retained"])
        self.assertIn("race_limitation_warning", res)

        # Verify recorded action
        mouse_actions = [
            a for a in driver.recorded_actions if a.get("action") == "mouse_click"
        ]
        self.assertEqual(len(mouse_actions), 1)
        self.assertEqual(mouse_actions[0]["screen_x"], 500)
        self.assertEqual(mouse_actions[0]["screen_y"], 300)

    def test_key_fails_if_unsupported_key(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        res = controller.key("nonexistent_key_12345")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "UNSUPPORTED_KEY")

    def test_key_fails_if_game_not_foreground_pre(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 99999

        res = controller.key("space")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "FOREGROUND_MISMATCH_PRE")

    def test_key_success_dispatches_correct_vk_code(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        res = controller.key("space")
        self.assertTrue(res["success"])
        self.assertEqual(res["vk_code"], "0x20")

        key_actions = [
            a for a in driver.recorded_actions if a.get("action") == "key_press"
        ]
        self.assertEqual(len(key_actions), 1)
        self.assertEqual(key_actions[0]["vk_code"], 0x20)

        res_f12 = controller.key("f12")
        self.assertTrue(res_f12["success"])
        self.assertEqual(res_f12["vk_code"], "0x7b")

    def test_krsdk_login_rejects_missing_env_credentials(self) -> None:
        driver, controller = create_standard_test_environment()

        with patch.dict(os.environ, {}, clear=True):
            res = controller.login_krsdk()
            self.assertFalse(res["success"])
            self.assertEqual(res["error"], "MISSING_CREDENTIALS")

    def test_krsdk_login_does_not_log_password(self) -> None:
        driver, controller = create_standard_test_environment()
        secret_pass = "SuperSecret_PGR_Pass123!"

        with patch.dict(
            os.environ,
            {"ASCNET_USERNAME": "testuser", "ASCNET_PASSWORD": secret_pass},
        ):
            res = controller.login_krsdk(timeout=2.0)
            res_json_str = json.dumps(res)

            self.assertNotIn(secret_pass, res_json_str)
            self.assertIn("testuser", res_json_str)

    def test_krsdk_login_success_flow(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.auth_simulation = "success"

        with patch.dict(
            os.environ,
            {"ASCNET_USERNAME": "testuser", "ASCNET_PASSWORD": "secretpassword"},
        ):
            res = controller.login_krsdk(timeout=2.0)

            self.assertTrue(res["success"])
            self.assertEqual(res["status"], "LOGIN_SUCCESS")
            self.assertEqual(res["username"], "testuser")
            self.assertEqual(driver.submit_count, 1)

            # Check that login and main windows were cleaned up upon successful dismissal
            self.assertNotIn(4001, driver.windows)
            self.assertNotIn(2001, driver.windows)
            self.assertNotIn(19596018, driver.windows)

    def test_krsdk_login_error_dialog_flow(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.auth_simulation = "error"
        driver.error_message = "Invalid password provided"

        with patch.dict(
            os.environ,
            {"ASCNET_USERNAME": "testuser", "ASCNET_PASSWORD": "wrongpassword"},
        ):
            res = controller.login_krsdk(timeout=2.0)

            self.assertFalse(res["success"])
            self.assertEqual(res["status"], "LOGIN_FAILED")
            self.assertEqual(res["error"], "AUTH_FAILED")
            self.assertIn("Invalid password", res["message"])
            self.assertEqual(driver.submit_count, 1)
            self.assertNotIn(4001, driver.windows)

    def test_krsdk_login_refuses_to_resubmit_with_pending_dialog(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.windows[4001] = FakeWindow(
            hwnd=4001,
            pid=20600,
            class_name="#32770",
            title="Success",
            owner_hwnd=19596018,
        )

        with patch.dict(
            os.environ,
            {"ASCNET_USERNAME": "testuser", "ASCNET_PASSWORD": "secretpassword"},
        ):
            res = controller.login_krsdk(timeout=1.0)

        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "PENDING_DIALOG")
        self.assertEqual(driver.submit_count, 0)

    def test_krsdk_login_timeout_uncertain_and_no_retry(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.auth_simulation = "timeout"  # Neither success nor error dialog appears

        with patch.dict(
            os.environ,
            {"ASCNET_USERNAME": "testuser", "ASCNET_PASSWORD": "anypassword"},
        ):
            res = controller.login_krsdk(timeout=0.3, poll_interval=0.05)

            self.assertFalse(res["success"])
            self.assertEqual(res["status"], "TIMEOUT_UNCERTAIN")
            self.assertEqual(res["error"], "SUBMISSION_TIMEOUT")
            self.assertIn("Outcome is uncertain", res["message"])
            self.assertIn("Submission was NOT retried", res["message"])
            # Verify exactly ONE submit was dispatched
            self.assertEqual(driver.submit_count, 1)

    def test_krsdk_login_pid_mismatch(self) -> None:
        driver, controller = create_standard_test_environment()
        # Set button 10001 to a different PID (hijacked control simulation)
        driver.windows[10001].pid = 99999

        with patch.dict(
            os.environ,
            {"ASCNET_USERNAME": "testuser", "ASCNET_PASSWORD": "secretpassword"},
        ):
            res = controller.login_krsdk(timeout=1.0)
            self.assertFalse(res["success"])
            self.assertEqual(res["error"], "PID_MISMATCH")

    def test_cli_main_commands(self) -> None:
        driver, _ = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        # Test CLI click
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["click", "--x", "100", "--y", "100"], driver=driver)
            self.assertEqual(code, 0)
        output = json.loads(buf.getvalue())
        self.assertTrue(output["success"])
        self.assertEqual(output["client_coords"], {"x": 100, "y": 100})

        # Test CLI key
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["key", "--key", "space"], driver=driver)
            self.assertEqual(code, 0)
        output = json.loads(buf.getvalue())
        self.assertTrue(output["success"])
        self.assertEqual(output["key"], "space")

        # Test CLI focus
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["focus", "--target", "game"], driver=driver)
            self.assertEqual(code, 0)
        output = json.loads(buf.getvalue())
        self.assertTrue(output["success"])

        # Test CLI screenshot
        out_path = Path(".runtime") / "cli_screenshot.png"
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["screenshot", "--out", str(out_path)], driver=driver)
            self.assertEqual(code, 0)
        output = json.loads(buf.getvalue())
        self.assertTrue(output["success"])
        if out_path.exists():
            out_path.unlink()


class TestPgrUiActions(unittest.TestCase):
    """Covers declarative named UI actions backed by pgr_ui_layout.json."""

    @staticmethod
    def _write_layout(directory: str, payload) -> str:
        path = Path(directory) / "layout.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    @staticmethod
    def _fixture_layout(actions: dict) -> dict:
        return {
            "schema": "pgr-ui-layout",
            "schemaVersion": 1,
            "actions": actions,
        }

    @staticmethod
    def _calibrated_entry(x: float = 0.5, y: float = 0.25) -> dict:
        return {
            "description": "Calibrated fixture action",
            "clientControl": "FixtureControl",
            "anchor": {"type": "normalized_point", "x": x, "y": y},
            "status": "calibrated",
            "calibrationSource": "unit test fixture",
            "dispatch": {"type": "click"},
            "verification": {"kind": "none"},
        }

    @staticmethod
    def _provisional_entry(anchor=None) -> dict:
        return {
            "description": "Provisional fixture action",
            "clientControl": "FixtureControl",
            "anchor": anchor,
            "status": "provisional",
            "dispatch": {"type": "click"},
            "verification": {"kind": "none"},
        }

    @staticmethod
    def _mouse_clicks(driver: FakeWin32Driver) -> list:
        return [
            a
            for a in driver.recorded_actions
            if a.get("action") == "mouse_click"
        ]

    def test_default_layout_file_is_valid_and_covers_route(self) -> None:
        layout = load_ui_layout()  # tracked Scripts/pgr_ui_layout.json
        self.assertEqual(layout["schema"], "pgr-ui-layout")
        self.assertEqual(layout["schemaVersion"], 1)
        actions = layout["actions"]

        expected = {
            "lobby.enter",
            "lobby.dismiss_neutral_top",
            "lobby.dismiss_left_margin",
            "lobby.dismiss_top_left",
            "main_terminal.bottom_bar_toggle",
            "main_terminal.camera_button",
            "photograph.btn_hide",
            "photograph.btn_scene",
            "photograph.scene_change_1",
            "photograph.scene_change_2",
            "photograph.scene_change_3",
            "photograph.scene_list",
            "scene_setting.open",
        }
        self.assertEqual(set(actions), expected)

        enter = actions["lobby.enter"]
        self.assertEqual(enter["status"], "calibrated")
        self.assertEqual(
            enter["anchor"],
            {"type": "normalized_point", "x": 0.5, "y": 0.5},
        )
        self.assertTrue(enter["calibrationSource"])

        for name in expected - {
            "lobby.enter",
            "lobby.dismiss_neutral_top",
            "lobby.dismiss_left_margin",
            "lobby.dismiss_top_left",
        }:
            self.assertEqual(actions[name]["status"], "provisional", name)
            self.assertIsNone(actions[name]["anchor"], name)
            self.assertTrue(actions[name]["clientControl"], name)

    def test_action_list_default_lists_only_calibrated(self) -> None:
        driver, controller = create_standard_test_environment()
        res = controller.action_list()

        self.assertTrue(res["success"])
        self.assertEqual(res["command"], "action list")
        self.assertEqual(res["schema_version"], 1)
        self.assertFalse(res["include_provisional"])
        listed_names = [a["name"] for a in res["actions"]]
        self.assertIn("lobby.enter", listed_names)
        self.assertNotIn("photograph.btn_hide", listed_names)
        for entry in res["actions"]:
            self.assertEqual(entry["status"], "calibrated")
            self.assertTrue(entry["anchor_set"])
        counts = res["counts"]
        self.assertEqual(counts["listed"], counts["calibrated"])
        self.assertEqual(
            counts["total"], counts["calibrated"] + counts["provisional"]
        )
        self.assertGreater(counts["provisional"], 0)

    def test_action_list_include_provisional(self) -> None:
        driver, controller = create_standard_test_environment()
        res = controller.action_list(include_provisional=True)

        self.assertTrue(res["success"])
        listed_names = [a["name"] for a in res["actions"]]
        self.assertEqual(res["counts"]["listed"], res["counts"]["total"])
        self.assertIn("photograph.btn_hide", listed_names)
        self.assertIn("scene_setting.open", listed_names)
        hide = next(
            a for a in res["actions"] if a["name"] == "photograph.btn_hide"
        )
        self.assertEqual(hide["status"], "provisional")
        self.assertFalse(hide["anchor_set"])

    def test_action_show_entries_and_unknown(self) -> None:
        driver, controller = create_standard_test_environment()

        res = controller.action_show("lobby.enter")
        self.assertTrue(res["success"])
        self.assertEqual(res["action"], "lobby.enter")
        self.assertEqual(res["entry"]["status"], "calibrated")
        self.assertTrue(res["anchor_set"])
        self.assertFalse(res["provisional_opt_in_required"])

        res_prov = controller.action_show("photograph.btn_scene")
        self.assertTrue(res_prov["success"])
        self.assertTrue(res_prov["provisional_opt_in_required"])
        self.assertFalse(res_prov["anchor_set"])
        self.assertIn("BtnScene", res_prov["entry"]["clientControl"])

        res_missing = controller.action_show("no.such.action")
        self.assertFalse(res_missing["success"])
        self.assertEqual(res_missing["error"], "ACTION_NOT_FOUND")
        self.assertIn("lobby.enter", res_missing["available_actions"])

    def test_action_invoke_calibrated_dispatches_exact_virtual_coords(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        res = controller.action_invoke("lobby.enter")
        self.assertTrue(res["success"])
        self.assertEqual(res["action"], "lobby.enter")
        self.assertEqual(res["action_status"], "calibrated")
        self.assertEqual(
            res["anchor"], {"space": "normalized", "x": 0.5, "y": 0.5}
        )
        # 0.5 * 1536 = 768, 0.5 * 960 = 480 in the virtualized client space.
        self.assertEqual(
            res["resolved_client"],
            {"space": "virtual", "x": 768, "y": 480},
        )
        self.assertEqual(
            res["dispatch"], {"type": "click", "dispatched": True}
        )
        self.assertEqual(res["verification"]["result"], "UNKNOWN")
        self.assertFalse(res["verification"]["performed"])
        self.assertTrue(res["foreground_retained"])
        self.assertIn("race_limitation_warning", res)

        clicks = self._mouse_clicks(driver)
        self.assertEqual(len(clicks), 1)
        # Fake client origin is (0,0): screen coords equal client coords.
        self.assertEqual(clicks[0]["screen_x"], 768)
        self.assertEqual(clicks[0]["screen_y"], 480)

    def test_action_invoke_provisional_refused_without_opt_in(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        res = controller.action_invoke("photograph.btn_hide")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "ACTION_PROVISIONAL")
        self.assertEqual(res["action_status"], "provisional")
        self.assertEqual(self._mouse_clicks(driver), [])

    def test_action_invoke_unset_anchor_fails_closed_even_with_opt_in(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        res = controller.action_invoke(
            "photograph.btn_hide", allow_provisional=True
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "ACTION_ANCHOR_UNSET")
        self.assertEqual(self._mouse_clicks(driver), [])

    def test_action_invoke_provisional_opt_in_dispatches(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        layout = self._fixture_layout(
            {
                "fixture.tap": self._provisional_entry(
                    anchor={"type": "normalized_point", "x": 0.25, "y": 0.5}
                )
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_layout(tmp, layout)
            res = controller.action_invoke(
                "fixture.tap", allow_provisional=True, layout_path=path
            )

        self.assertTrue(res["success"])
        self.assertEqual(res["action_status"], "provisional")
        self.assertEqual(
            res["resolved_client"],
            {"space": "virtual", "x": 384, "y": 480},
        )
        self.assertTrue(res["dispatch"]["dispatched"])
        self.assertEqual(res["verification"]["result"], "UNKNOWN")
        self.assertTrue(
            any("PROVISIONAL" in w for w in res["warnings"]),
            res["warnings"],
        )
        clicks = self._mouse_clicks(driver)
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0]["screen_x"], 384)
        self.assertEqual(clicks[0]["screen_y"], 480)

    def test_action_invoke_unknown_action(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        res = controller.action_invoke("nonexistent.action")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "ACTION_NOT_FOUND")
        self.assertIn("lobby.enter", res["available_actions"])
        self.assertEqual(self._mouse_clicks(driver), [])

    def test_action_invoke_anchor_resolving_out_of_bounds(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        # 0.99999 * 1536 rounds to 1536, which is >= the client width.
        layout = self._fixture_layout(
            {"edge.bounds": self._calibrated_entry(x=0.99999, y=0.5)}
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_layout(tmp, layout)
            res = controller.action_invoke("edge.bounds", layout_path=path)

        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "ANCHOR_OUT_OF_BOUNDS")
        self.assertEqual(res["resolved_client"]["x"], 1536)
        self.assertEqual(self._mouse_clicks(driver), [])

    def test_action_layout_not_found(self) -> None:
        driver, controller = create_standard_test_environment()
        missing = str(Path(".runtime") / "definitely_missing_layout.json")

        res_list = controller.action_list(layout_path=missing)
        self.assertFalse(res_list["success"])
        self.assertEqual(res_list["error"], "LAYOUT_NOT_FOUND")

        res_invoke = controller.action_invoke(
            "lobby.enter", layout_path=missing
        )
        self.assertFalse(res_invoke["success"])
        self.assertEqual(res_invoke["error"], "LAYOUT_NOT_FOUND")

    def test_action_layout_invalid_version(self) -> None:
        driver, controller = create_standard_test_environment()

        with tempfile.TemporaryDirectory() as tmp:
            for label, mutate in (
                ("unsupported_version", {"schemaVersion": 2}),
                ("missing_version", {"schemaVersion": None}),
                ("wrong_schema", {"schema": "other-schema"}),
            ):
                layout = self._fixture_layout(
                    {"fixture.tap": self._calibrated_entry()}
                )
                for key, value in mutate.items():
                    if value is None:
                        layout.pop(key, None)
                    else:
                        layout[key] = value
                path = self._write_layout(
                    tmp, layout
                )
                res = controller.action_list(layout_path=path)
                self.assertFalse(res["success"], label)
                self.assertEqual(res["error"], "LAYOUT_INVALID", label)

    def test_action_layout_rejects_nan_and_out_of_range(self) -> None:
        driver, controller = create_standard_test_environment()

        with tempfile.TemporaryDirectory() as tmp:
            # Python's json accepts NaN by default; the loader must reject it.
            nan_text = json.dumps(
                self._fixture_layout(
                    {"fixture.nan": self._calibrated_entry()}
                )
            ).replace('"x": 0.5', '"x": NaN')
            self.assertIn("NaN", nan_text)
            res = controller.action_list(
                layout_path=self._write_layout(tmp, nan_text)
            )
            self.assertFalse(res["success"])
            self.assertEqual(res["error"], "LAYOUT_INVALID")

            for label, x, y in (
                ("x_at_one", 1.0, 0.5),
                ("x_above_one", 1.5, 0.5),
                ("x_negative", -0.25, 0.5),
                ("y_negative", 0.5, -0.1),
            ):
                layout = self._fixture_layout(
                    {"edge.range": self._calibrated_entry(x=x, y=y)}
                )
                res = controller.action_list(
                    layout_path=self._write_layout(tmp, layout)
                )
                self.assertFalse(res["success"], label)
                self.assertEqual(res["error"], "LAYOUT_INVALID", label)

    def test_action_layout_malformed(self) -> None:
        driver, controller = create_standard_test_environment()

        with tempfile.TemporaryDirectory() as tmp:
            cases = {
                "not_json": "{not valid json",
                "top_level_array": "[]",
                "missing_anchor": self._fixture_layout(
                    {
                        "bad.entry": {
                            k: v
                            for k, v in self._calibrated_entry().items()
                            if k != "anchor"
                        }
                    }
                ),
                "unknown_field": self._fixture_layout(
                    {
                        "bad.entry": {
                            **self._calibrated_entry(),
                            "surprise": True,
                        }
                    }
                ),
                "string_coordinate": self._fixture_layout(
                    {
                        "bad.entry": {
                            **self._calibrated_entry(),
                            "anchor": {
                                "type": "normalized_point",
                                "x": "0.5",
                                "y": 0.5,
                            },
                        }
                    }
                ),
                "calibrated_without_source": self._fixture_layout(
                    {
                        "bad.entry": {
                            k: v
                            for k, v in self._calibrated_entry().items()
                            if k != "calibrationSource"
                        }
                    }
                ),
                "calibrated_unset_anchor": self._fixture_layout(
                    {
                        "bad.entry": {
                            **self._calibrated_entry(),
                            "anchor": None,
                        }
                    }
                ),
                "unknown_status": self._fixture_layout(
                    {
                        "bad.entry": {
                            **self._calibrated_entry(),
                            "status": "verified",
                        }
                    }
                ),
            }
            for label, payload in cases.items():
                res = controller.action_list(
                    layout_path=self._write_layout(tmp, payload)
                )
                self.assertFalse(res["success"], label)
                self.assertEqual(res["error"], "LAYOUT_INVALID", label)

    def test_action_invoke_propagates_click_guard_failure(self) -> None:
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 99999  # unrelated window holds foreground

        res = controller.action_invoke("lobby.enter")
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "FOREGROUND_MISMATCH_PRE")
        self.assertEqual(
            res["dispatch"], {"type": "click", "dispatched": False}
        )
        self.assertEqual(res["verification"]["result"], "UNKNOWN")
        self.assertEqual(self._mouse_clicks(driver), [])

    def test_action_cli_wiring(self) -> None:
        driver, _ = create_standard_test_environment()

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["action", "list"], driver=driver)
        self.assertEqual(code, 0)
        output = json.loads(buf.getvalue())
        self.assertTrue(output["success"])
        self.assertEqual(output["command"], "action list")

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(
                ["action", "list", "--include-provisional"], driver=driver
            )
        self.assertEqual(code, 0)
        output = json.loads(buf.getvalue())
        self.assertTrue(output["include_provisional"])
        self.assertEqual(
            output["counts"]["listed"], output["counts"]["total"]
        )

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["action", "show", "lobby.enter"], driver=driver)
        self.assertEqual(code, 0)
        output = json.loads(buf.getvalue())
        self.assertEqual(output["entry"]["status"], "calibrated")

        driver.foreground_hwnd = 5770964
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(
                ["action", "invoke", "lobby.enter"], driver=driver
            )
        self.assertEqual(code, 0)
        output = json.loads(buf.getvalue())
        self.assertEqual(
            output["resolved_client"],
            {"space": "virtual", "x": 768, "y": 480},
        )

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(
                ["action", "invoke", "photograph.btn_hide"], driver=driver
            )
        self.assertEqual(code, 1)
        output = json.loads(buf.getvalue())
        self.assertEqual(output["error"], "ACTION_PROVISIONAL")

        layout = self._fixture_layout(
            {
                "fixture.tap": self._provisional_entry(
                    anchor={"type": "normalized_point", "x": 0.25, "y": 0.5}
                )
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_layout(tmp, layout)
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                code = main(
                    [
                        "action",
                        "invoke",
                        "fixture.tap",
                        "--allow-provisional",
                        "--layout",
                        path,
                    ],
                    driver=driver,
                )
        self.assertEqual(code, 0)
        output = json.loads(buf.getvalue())
        self.assertTrue(output["dispatch"]["dispatched"])

        # Bare `action` (no subcommand) is an argparse usage error (exit 2).
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["action"], driver=driver)
        self.assertEqual(ctx.exception.code, 2)

    def test_action_layout_rejects_unhashable_field_values(self) -> None:
        """Unhashable JSON list/dict values must yield LAYOUT_INVALID.

        Regression: frozenset membership tests hash the operand, so a
        list/dict `status`, `anchor.type`, `dispatch.type`, or
        `verification.kind` previously escaped as an uncaught TypeError
        instead of the structured loader error.
        """
        driver, controller = create_standard_test_environment()
        base = self._calibrated_entry()
        cases = {
            "status_list": {**base, "status": []},
            "status_dict": {**base, "status": {}},
            "anchor_type_list": {
                **base,
                "anchor": {"type": [], "x": 0.5, "y": 0.5},
            },
            "anchor_type_dict": {
                **base,
                "anchor": {"type": {}, "x": 0.5, "y": 0.5},
            },
            "dispatch_type_list": {**base, "dispatch": {"type": ["click"]}},
            "dispatch_type_dict": {**base, "dispatch": {"type": {"t": 1}}},
            "verification_kind_list": {
                **base,
                "verification": {"kind": []},
            },
            "verification_kind_dict": {
                **base,
                "verification": {"kind": {"k": 1}},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, entry in cases.items():
                layout = self._fixture_layout({"bad.entry": entry})
                path = self._write_layout(tmp, layout)
                for command in ("list", "show", "invoke"):
                    if command == "list":
                        res = controller.action_list(layout_path=path)
                    elif command == "show":
                        res = controller.action_show(
                            "bad.entry", layout_path=path
                        )
                    else:
                        res = controller.action_invoke(
                            "bad.entry", layout_path=path
                        )
                    self.assertFalse(res["success"], (label, command))
                    self.assertEqual(
                        res["error"], "LAYOUT_INVALID", (label, command)
                    )
        self.assertEqual(self._mouse_clicks(driver), [])

    def test_action_layout_rejects_huge_integer_coordinate(self) -> None:
        """Arbitrary-length JSON ints must not escape math.isfinite.

        Regression: math.isfinite(10**400) raises OverflowError; the range
        comparison must reject huge ints before any float conversion.
        """
        driver, controller = create_standard_test_environment()

        with tempfile.TemporaryDirectory() as tmp:
            layout = self._fixture_layout(
                {"edge.huge": self._calibrated_entry(x=10**400, y=0.5)}
            )
            res = controller.action_list(
                layout_path=self._write_layout(tmp, layout)
            )
            self.assertFalse(res["success"])
            self.assertEqual(res["error"], "LAYOUT_INVALID")

            # Exponent overflow (1e999) parses to inf; same rejection path.
            inf_text = json.dumps(
                self._fixture_layout(
                    {"edge.inf": self._calibrated_entry()}
                )
            ).replace('"x": 0.5', '"x": 1e999')
            res = controller.action_list(
                layout_path=self._write_layout(tmp, inf_text)
            )
            self.assertFalse(res["success"])
            self.assertEqual(res["error"], "LAYOUT_INVALID")

    def test_action_layout_verification_key_allowlist(self) -> None:
        """Verification objects enforce a strict per-kind key allowlist."""
        driver, controller = create_standard_test_environment()
        base = self._calibrated_entry()

        invalid = {
            "none_extra_key": {"kind": "none", "bogus": 1},
            "none_with_ratio": {"kind": "none", "minChangeRatio": 0.5},
            "diff_extra_key": {"kind": "screenshot-diff", "bogus": 1},
            "diff_ratio_string": {
                "kind": "screenshot-diff",
                "minChangeRatio": "big",
            },
            "diff_ratio_bool": {
                "kind": "screenshot-diff",
                "minChangeRatio": True,
            },
            "diff_ratio_zero": {"kind": "screenshot-diff", "minChangeRatio": 0},
            "diff_ratio_negative": {
                "kind": "screenshot-diff",
                "minChangeRatio": -0.5,
            },
            "diff_ratio_above_one": {
                "kind": "screenshot-diff",
                "minChangeRatio": 1.5,
            },
            "diff_ratio_huge_int": {
                "kind": "screenshot-diff",
                "minChangeRatio": 10**400,
            },
        }
        valid = {
            "none_bare": {"kind": "none"},
            "diff_bare": {"kind": "screenshot-diff"},
            "diff_ratio_valid": {
                "kind": "screenshot-diff",
                "minChangeRatio": 0.01,
            },
            "diff_ratio_boundary": {
                "kind": "screenshot-diff",
                "minChangeRatio": 1,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, verification in invalid.items():
                layout = self._fixture_layout(
                    {"bad.entry": {**base, "verification": verification}}
                )
                res = controller.action_list(
                    layout_path=self._write_layout(tmp, layout)
                )
                self.assertFalse(res["success"], label)
                self.assertEqual(res["error"], "LAYOUT_INVALID", label)

            for label, verification in valid.items():
                layout = self._fixture_layout(
                    {"ok.entry": {**base, "verification": verification}}
                )
                res = controller.action_list(
                    layout_path=self._write_layout(tmp, layout)
                )
                self.assertTrue(res["success"], label)

    def test_action_invoke_screenshot_diff_stays_declarative(self) -> None:
        """A declared screenshot-diff verification is never executed here."""
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        entry = {
            **self._calibrated_entry(),
            "verification": {"kind": "screenshot-diff", "minChangeRatio": 0.01},
        }
        layout = self._fixture_layout({"fixture.tap": entry})
        with tempfile.TemporaryDirectory() as tmp:
            res = controller.action_invoke(
                "fixture.tap", layout_path=self._write_layout(tmp, layout)
            )

        self.assertTrue(res["success"])
        self.assertEqual(res["verification"]["kind"], "screenshot-diff")
        self.assertFalse(res["verification"]["performed"])
        self.assertEqual(res["verification"]["result"], "UNKNOWN")
        grabs = [
            a for a in driver.recorded_actions if a.get("action") == "grab_screen"
        ]
        self.assertEqual(grabs, [])

    def test_action_invoke_uses_single_discovery_snapshot(self) -> None:
        """Invoke resolves and dispatches on ONE discover() snapshot.

        Regression: the anchor used to resolve against snapshot A while the
        delegated click re-discovered snapshot B; a mid-invocation resize or
        window change could produce a stale-offset or wrong-window dispatch.
        """
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        discover_calls = []
        real_discover = controller.discover

        def counted_discover():
            data = real_discover()
            discover_calls.append(data)
            if len(discover_calls) > 1:
                # A second snapshot simulates a mid-invocation resize and a
                # different target window: shrunken client rect, new hwnd.
                stale_game = dict(data["windows"]["game"])
                stale_game["hwnd"] = 424242
                stale_game["client_rect"] = {
                    "left": 0,
                    "top": 0,
                    "right": 768,
                    "bottom": 480,
                    "width": 768,
                    "height": 480,
                }
                data["windows"]["game"] = stale_game
            return data

        controller.discover = counted_discover

        res = controller.action_invoke("lobby.enter")
        self.assertTrue(res["success"])
        self.assertEqual(len(discover_calls), 1)
        # Resolution and dispatch both used snapshot A: 0.5 * 1536 = 768,
        # 0.5 * 960 = 480 against the original window.
        self.assertEqual(
            res["resolved_client"], {"space": "virtual", "x": 768, "y": 480}
        )
        self.assertEqual(res["target_hwnd"], 5770964)
        clicks = self._mouse_clicks(driver)
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0]["screen_x"], 768)
        self.assertEqual(clicks[0]["screen_y"], 480)

    def test_action_invoke_provisional_warning_never_claims_dispatch(self) -> None:
        """The opt-in warning must not assert a dispatch that never ran."""
        driver, controller = create_standard_test_environment()
        driver.foreground_hwnd = 5770964

        res = controller.action_invoke(
            "photograph.btn_hide", allow_provisional=True
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["error"], "ACTION_ANCHOR_UNSET")
        self.assertEqual(self._mouse_clicks(driver), [])
        for warning in res["warnings"]:
            self.assertNotIn("was dispatched", warning)


if __name__ == "__main__":
    unittest.main()
