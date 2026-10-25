from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maiwill.phone import AdbPhone, normalize_device_address


class PhoneAdapterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.adb = self.root / "adb.exe"
        self.adb.write_bytes(b"test placeholder; never executed")
        self.phone = AdbPhone(str(self.adb), "127.0.0.1:16384", self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_address_accepts_ip_literals_and_rejects_hostnames(self) -> None:
        self.assertEqual(normalize_device_address("127.0.0.1:16384"), "127.0.0.1:16384")
        self.assertEqual(normalize_device_address("[2001:db8::1]:5555"), "[2001:db8::1]:5555")
        for address in ("localhost:5555", "127.0.0.1:0", "127.0.0.1:65536", "[::1]"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                normalize_device_address(address)

    async def test_unconfigured_phone_does_not_start_adb(self) -> None:
        result = await AdbPhone("", "127.0.0.1:16384", self.root).use({"action": "status"})
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "not_configured")

    async def test_commands_always_use_the_configured_device_and_reject_selector_override(self) -> None:
        calls = []

        async def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            if args == ("exec-out", "screencap", "-p"):
                return b"\x89PNG\r\n\x1a\nmock image"
            return b""

        self.phone._run = fake_run  # type: ignore[method-assign]

        async def no_wait(_seconds):
            return None

        with patch("maiwill.phone.asyncio.sleep", no_wait):
            result = await self.phone.use({"action": "back", "settle_ms": 0})
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "screenshot")
        self.assertEqual(calls[0][0], ("connect", "127.0.0.1:16384"))
        self.assertFalse(calls[0][1]["select_device"])
        self.assertEqual(calls[1][0], ("shell", "input keyevent 4"))
        self.assertEqual(calls[1][1], {"timeout": None})
        self.assertEqual(calls[2][0], ("exec-out", "screencap", "-p"))

        rejected = await self.phone.use({"action": "adb", "arguments": ["shell", "-s", "other-device"]})
        self.assertFalse(rejected["success"])
        self.assertIn("不能修改配置设备", rejected["error"])
        self.assertEqual(len(calls), 3)

        host_command = await self.phone.use({"action": "adb", "arguments": ["devices"]})
        self.assertFalse(host_command["success"])
        transport_override = await self.phone.use(
            {"action": "adb", "arguments": ["shell", "--transport-id=2", "id"]}
        )
        host_file = await self.phone.use({"action": "adb", "arguments": ["pull", "/sdcard/file", "C:\\temp\\file"]})
        self.assertFalse(transport_override["success"])
        self.assertTrue(host_file["success"])
        self.assertEqual(len(calls), 5)

        for command, args in (("pull", ["/sdcard/Download/report.txt", "C:\\Temp\\report.txt"]),
                              ("push", ["C:\\Temp\\input.txt", "/sdcard/Download/input.txt"])):
            with self.subTest(command=command):
                result = await self.phone.use({"action": "adb", "arguments": [command, *args]})
                self.assertTrue(result["success"])
        file_transfers = [call for call in calls if call[0][0] in {"pull", "push"}]
        file_transfers = file_transfers[1:]
        self.assertEqual(file_transfers[0][0], ("pull", "/sdcard/Download/report.txt", "C:\\Temp\\report.txt"))
        self.assertEqual(file_transfers[0][1]["timeout"], 60)
        self.assertEqual(file_transfers[1][0], ("push", "C:\\Temp\\input.txt", "/sdcard/Download/input.txt"))
        self.assertEqual(file_transfers[1][1]["timeout"], 60)
        invalid_text = await self.phone.use({"action": "type", "text": "你好"})
        self.assertFalse(invalid_text["success"])
        self.assertEqual(len(calls), 9)

    async def test_invalid_coordinates_and_unknown_actions_never_reach_adb(self) -> None:
        calls = []

        async def fake_shell(*args, **kwargs):
            calls.append(args)
            return ""

        async def fake_run(*args, **kwargs):
            return b""

        self.phone._run = fake_run  # type: ignore[method-assign]
        self.phone._shell = fake_shell  # type: ignore[method-assign]
        invalid_tap = await self.phone.use({"action": "tap", "x": -1, "y": 20})
        unknown = await self.phone.use({"action": "teleport"})
        self.assertFalse(invalid_tap["success"])
        self.assertFalse(unknown["success"])
        self.assertEqual(calls, [])

    async def test_screenshot_is_saved_under_the_plugin_data_directory(self) -> None:
        async def fake_run(*args, **kwargs):
            if args == ("exec-out", "screencap", "-p"):
                return b"\x89PNG\r\n\x1a\nmock image"
            self.assertEqual(args, ("connect", "127.0.0.1:16384"))
            return b""

        self.phone._run = fake_run  # type: ignore[method-assign]
        result = await self.phone.use({"action": "screenshot"})
        self.assertTrue(result["success"])
        image_path = self.root / result["path"]
        self.assertTrue(image_path.is_file())
        self.assertEqual(image_path.read_bytes(), b"\x89PNG\r\n\x1a\nmock image")

    async def test_drag_runs_and_automatically_returns_post_action_screenshot(self) -> None:
        calls = []

        async def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            if args == ("exec-out", "screencap", "-p"):
                return b"\x89PNG\r\n\x1a\nmock image"
            return b""

        async def fake_shell(command, *, timeout=None):
            calls.append((("shell", command), {"timeout": timeout}))
            return ""

        async def no_wait(_seconds):
            return None

        self.phone._run = fake_run  # type: ignore[method-assign]
        self.phone._shell = fake_shell  # type: ignore[method-assign]
        with patch("maiwill.phone.asyncio.sleep", no_wait):
            result = await self.phone.use({
                "action": "drag", "x1": 100, "y1": 200, "x2": 500, "y2": 450,
                "settle_ms": 0,
            })

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "screenshot")
        self.assertEqual(result["action"], "drag")
        self.assertEqual(calls[0][0], ("connect", "127.0.0.1:16384"))
        self.assertEqual(calls[1][0], ("shell", "input swipe 100 200 500 450 500"))
        self.assertEqual(calls[2][0], ("exec-out", "screencap", "-p"))
        self.assertTrue((self.root / result["path"]).is_file())

    async def test_instant_drag_injects_down_move_up_without_intermediate_path(self) -> None:
        calls = []

        async def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            if args == ("exec-out", "screencap", "-p"):
                return b"\x89PNG\r\n\x1a\nmock image"
            return b""

        async def fake_shell(command, *, timeout=None):
            calls.append((("shell", command), {"timeout": timeout}))
            return ""

        async def no_wait(_seconds):
            return None

        self.phone._run = fake_run  # type: ignore[method-assign]
        self.phone._shell = fake_shell  # type: ignore[method-assign]
        with patch("maiwill.phone.asyncio.sleep", no_wait):
            result = await self.phone.use({
                "action": "instant_drag", "x1": 120, "y1": 640,
                "x2": 1100, "y2": 95, "settle_ms": 0,
            })
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "screenshot")
        self.assertEqual(result["action"], "instant_drag")
        self.assertEqual(calls[1][0], (
            "shell",
            "input motionevent DOWN 120 640; down_status=$?; "
            "input motionevent MOVE 1100 95; move_status=$?; "
            "input motionevent UP 1100 95; up_status=$?; "
            "test $down_status -eq 0 && test $move_status -eq 0 && test $up_status -eq 0",
        ))
        self.assertEqual(calls[2][0], ("exec-out", "screencap", "-p"))

    async def test_instant_drag_attempts_release_even_if_move_event_fails(self) -> None:
        commands = []

        async def fake_run(*args, **kwargs):
            if args == ("exec-out", "screencap", "-p"):
                return b"\x89PNG\r\n\x1a\nmock image"
            return b""

        async def fake_shell(command, *, timeout=None):
            commands.append(command)
            return ""

        self.phone._run = fake_run  # type: ignore[method-assign]
        self.phone._shell = fake_shell  # type: ignore[method-assign]
        result = await self.phone.use({
            "action": "instant_drag", "x1": 10, "y1": 20, "x2": 30, "y2": 40,
        })
        self.assertTrue(result["success"])
        self.assertIn("input motionevent UP 30 40", commands[0])

    async def test_scroll_uses_screen_dimensions_and_invalid_gestures_are_rejected(self) -> None:
        commands = []

        async def fake_run(*args, **kwargs):
            if args == ("exec-out", "screencap", "-p"):
                return b"\x89PNG\r\n\x1a\nmock image"
            return b""

        async def fake_shell(command, *, timeout=None):
            commands.append(command)
            if command == "dumpsys input":
                return "Viewport INTERNAL: displayId=0 logicalFrame=[0, 0, 1080, 2400] physicalFrame=[0,0,1080,2400]\n"
            return ""

        async def no_wait(_seconds):
            return None

        self.phone._run = fake_run  # type: ignore[method-assign]
        self.phone._shell = fake_shell  # type: ignore[method-assign]
        with patch("maiwill.phone.asyncio.sleep", no_wait):
            result = await self.phone.use({"action": "scroll", "direction": "up", "distance_px": 800, "settle_ms": 0})
            invalid = await self.phone.use({"action": "scroll", "direction": "diagonal"})
        self.assertEqual(commands[:2], ["dumpsys input", "input swipe 540 1600 540 800 450"])
        self.assertEqual(result["status"], "screenshot")
        self.assertFalse(invalid["success"])
        self.assertIn("direction", invalid["error"])
        tiny_delta = await self.phone.use({"action": "scroll", "direction": "up", "distance_px": 1, "settle_ms": 0})
        self.assertTrue(tiny_delta["success"])
        self.assertEqual(commands[-2], "dumpsys input")
        self.assertEqual(commands[-1], "input swipe 540 1201 540 1199 450")

    async def test_scroll_uses_rotated_logical_input_dimensions_for_landscape_games(self) -> None:
        commands = []

        async def fake_run(*args, **kwargs):
            if args == ("exec-out", "screencap", "-p"):
                return b"\x89PNG\r\n\x1a\nmock image"
            return b""

        async def fake_shell(command, *, timeout=None):
            commands.append(command)
            if command == "dumpsys input":
                return "Viewport INTERNAL: displayId=0 logicalFrame=[0, 0, 1280, 720] physicalFrame=[0,0,1280,720]\n"
            return ""

        async def no_wait(_seconds):
            return None

        self.phone._run = fake_run  # type: ignore[method-assign]
        self.phone._shell = fake_shell  # type: ignore[method-assign]
        with patch("maiwill.phone.asyncio.sleep", no_wait):
            result = await self.phone.use({"action": "scroll", "direction": "right", "distance_px": 800, "settle_ms": 0})
        self.assertTrue(result["success"])
        self.assertEqual(commands[:2], ["dumpsys input", "input swipe 240 360 1040 360 450"])

    async def test_long_press_double_tap_and_wait_use_expected_input_events(self) -> None:
        commands = []

        async def fake_run(*args, **kwargs):
            if args == ("exec-out", "screencap", "-p"):
                return b"\x89PNG\r\n\x1a\nmock image"
            return b""

        async def fake_shell(command, *, timeout=None):
            commands.append(command)
            return ""

        async def no_wait(_seconds):
            return None

        self.phone._run = fake_run  # type: ignore[method-assign]
        self.phone._shell = fake_shell  # type: ignore[method-assign]
        with patch("maiwill.phone.asyncio.sleep", no_wait):
            long_press = await self.phone.use({"action": "long_press", "x": 42, "y": 84, "duration_ms": 1200, "settle_ms": 0})
            double_tap = await self.phone.use({"action": "double_tap", "x": 25, "y": 35, "settle_ms": 0})
            wait = await self.phone.use({"action": "wait", "wait_ms": 1500})

        self.assertEqual(commands, [
            "input swipe 42 84 42 84 1200",
            "input tap 25 35", "input tap 25 35",
        ])
        self.assertTrue(all(item["status"] == "screenshot" for item in (long_press, double_tap, wait)))
        self.assertEqual(wait["action"], "wait")

    async def test_gesture_screenshot_failure_says_action_completed_and_does_not_retry_it(self) -> None:
        commands = []

        async def fake_run(*args, **kwargs):
            commands.append(args)
            if args == ("exec-out", "screencap", "-p"):
                raise RuntimeError("device offline")
            return b""

        async def fake_shell(command, *, timeout=None):
            commands.append(("shell", command))
            return ""

        async def no_wait(_seconds):
            return None

        self.phone._run = fake_run  # type: ignore[method-assign]
        self.phone._shell = fake_shell  # type: ignore[method-assign]
        with patch("maiwill.phone.asyncio.sleep", no_wait):
            result = await self.phone.use({"action": "tap", "x": 10, "y": 20, "settle_ms": 0})
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("observation_error", result)
        self.assertEqual(commands.count(("shell", "input tap 10 20")), 1)

    async def test_screen_reads_and_limits_untrusted_ui_automator_nodes(self) -> None:
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
            '<hierarchy><node text="LABEL" content-desc="ACCESSIBILITY" clickable="true" bounds="[0,0][40,40]"/>'
            '<node text="" content-desc="" clickable="false" bounds=""/></hierarchy>'
        )
        commands = []

        async def fake_shell(command, *, timeout=None):
            commands.append(command)
            if command.startswith("uiautomator dump"):
                return "UI hierchary dumped to: " + command.rsplit(" ", 1)[-1]
            if command.startswith("cat " ):
                return xml
            return ""

        async def fake_run(*args, **kwargs):
            return b""

        self.phone._run = fake_run  # type: ignore[method-assign]
        self.phone._shell = fake_shell  # type: ignore[method-assign]
        result = await self.phone.use({"action": "screen"})
        self.assertTrue(result["success"])
        self.assertEqual(result["elements"][0]["text"], "LABEL")
        self.assertEqual(result["elements"][0]["description"], "ACCESSIBILITY")
        self.assertEqual(len(result["elements"]), 1)
        self.assertTrue(all(command.startswith(("uiautomator dump", "cat /sdcard/maiwill_", "rm -f /sdcard/maiwill_")) for command in commands))
        self.assertEqual(len(commands), 3)

    async def test_text_action_rejects_non_ascii_before_adb(self) -> None:
        calls = []

        async def fake_run(*args, **kwargs):
            return b""

        async def fake_shell(command, *, timeout=None):
            calls.append(command)
            return ""

        self.phone._run = fake_run  # type: ignore[method-assign]
        self.phone._shell = fake_shell  # type: ignore[method-assign]
        result = await self.phone.use({"action": "type", "text": "你好"})
        self.assertFalse(result["success"])
        self.assertEqual(calls, [])

    async def test_screenshot_rejects_symlinked_output_directory(self) -> None:
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        (self.root / "phone").mkdir()
        try:
            (self.root / "phone" / "screenshots").symlink_to(
                outside, target_is_directory=True
            )
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are unavailable in this environment")

        async def fake_run(*args, **kwargs):
            if args == ("exec-out", "screencap", "-p"):
                return b"\x89PNG\r\n\x1a\nmock image"
            return b""

        self.phone._run = fake_run  # type: ignore[method-assign]
        result = await self.phone.use({"action": "screenshot"})
        self.assertFalse(result["success"])
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
