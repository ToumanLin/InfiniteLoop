#!/usr/bin/env python3
"""Focused unit test suite for external Windows PGR window control CLI."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from Scripts.pgr_window_control import (
    FakeWin32Driver,
    FakeWindow,
    PgrWindowController,
    build_parser,
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

    # Game Window
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


if __name__ == "__main__":
    unittest.main()
