from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
            return b""

        self.phone._run = fake_run  # type: ignore[method-assign]

        result = await self.phone.use({"action": "back"})
        self.assertTrue(result["success"])
        self.assertEqual(calls[0][0], ("connect", "127.0.0.1:16384"))
        self.assertFalse(calls[0][1]["select_device"])
        self.assertEqual(calls[1][0], ("shell", "input keyevent 4"))
        self.assertEqual(calls[1][1], {"timeout": None})

        rejected = await self.phone.use({"action": "adb", "arguments": ["shell", "-s", "other-device"]})
        self.assertFalse(rejected["success"])
        self.assertIn("不能修改配置设备", rejected["error"])
        self.assertEqual(len(calls), 2)

        host_command = await self.phone.use({"action": "adb", "arguments": ["devices"]})
        self.assertFalse(host_command["success"])
        transport_override = await self.phone.use(
            {"action": "adb", "arguments": ["shell", "--transport-id=2", "id"]}
        )
        host_file = await self.phone.use({"action": "adb", "arguments": ["pull", "/sdcard/file", "C:\\temp\\file"]})
        self.assertFalse(transport_override["success"])
        self.assertFalse(host_file["success"])
        self.assertEqual(len(calls), 2)
        invalid_text = await self.phone.use({"action": "type", "text": "你好"})
        self.assertFalse(invalid_text["success"])
        self.assertEqual(len(calls), 2)

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
