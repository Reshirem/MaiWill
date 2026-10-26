from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from maiwill.vision import (
    MAX_VISUAL_ATTEMPTS,
    latest_pending_screenshot,
    load_screenshot_png,
    estimate_prompt_image_tokens,
    mark_screenshot_attempted,
    compact_consumed_screenshot,
)

SCREENSHOT_PATH = "phone/screenshots/0123456789abcdef0123456789abcdef.png"


def png_header(width: int = 320, height: int = 240) -> bytes:
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0))
    return b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + ihdr + b"\0\0\0\0"


class VisionInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "phone" / "screenshots").mkdir(parents=True)
        self.image = png_header()
        (self.root / SCREENSHOT_PATH).write_bytes(self.image)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_loads_only_bounded_png_from_dedicated_screenshot_directory(self) -> None:
        self.assertEqual(load_screenshot_png(self.root, SCREENSHOT_PATH), self.image)
        for invalid_path in ("../secret.png", "phone/screenshots/other.png", "phone/screenshots/0123456789abcdef0123456789abcdef.jpg"):
            with self.subTest(path=invalid_path), self.assertRaises(ValueError):
                load_screenshot_png(self.root, invalid_path)

    def test_rejects_invalid_png_header_and_excessive_dimensions(self) -> None:
        target = self.root / SCREENSHOT_PATH
        target.write_bytes(b"x" * 40)
        with self.assertRaisesRegex(ValueError, "PNG"):
            load_screenshot_png(self.root, SCREENSHOT_PATH)
        target.write_bytes(png_header(width=9000, height=200))
        with self.assertRaisesRegex(ValueError, "分辨率"): 
            load_screenshot_png(self.root, SCREENSHOT_PATH)

    def test_screenshot_attempts_are_bounded(self) -> None:
        screenshot = {"success": True, "status": "screenshot", "path": SCREENSHOT_PATH}
        context = {"messages": [{"role": "capability_result", "capability": "phone.use", "result": screenshot}]}
        self.assertIs(latest_pending_screenshot(context), screenshot)
        mark_screenshot_attempted(context, screenshot)
        self.assertIs(latest_pending_screenshot(context), screenshot)
        mark_screenshot_attempted(context, screenshot)
        self.assertIs(latest_pending_screenshot(context), screenshot)
        self.assertEqual(screenshot["visual_attempts"], MAX_VISUAL_ATTEMPTS)

    def test_successful_decision_compacts_screenshot_reference(self) -> None:
        screenshot = {"success": True, "status": "screenshot", "path": SCREENSHOT_PATH}
        context = {"messages": [{"role": "capability_result", "capability": "phone.use", "result": screenshot}]}
        mark_screenshot_attempted(context, screenshot)
        self.assertIs(latest_pending_screenshot(context), screenshot)
        compact_consumed_screenshot(context, screenshot)
        self.assertNotIn("image_base64", str(context))
        self.assertNotIn("path", context["messages"][0]["result"])
        self.assertIsNone(latest_pending_screenshot(context))

    def test_new_phone_action_invalidates_older_screenshot(self) -> None:
        screenshot = {"success": True, "status": "screenshot", "path": SCREENSHOT_PATH}
        context = {"messages": [
            {"role": "capability_result", "capability": "phone.use", "result": screenshot},
            {"role": "capability_result", "capability": "phone.use", "result": {"success": True, "status": "screen"}},
        ]}
        self.assertIsNone(latest_pending_screenshot(context))

    def test_image_prompt_budget_uses_dimensions_not_base64_length(self) -> None:
        self.assertGreater(estimate_prompt_image_tokens(png_header(1920, 1080)), 256)

    def test_direct_adb_screenshot_can_be_consumed_and_commands_expire_old_images(self) -> None:
        for capability in ("phone.use", "phone.adb"):
            screenshot = {"success": True, "status": "screenshot", "path": SCREENSHOT_PATH}
            context = {"messages": [{"role": "capability_result", "capability": capability, "result": screenshot}]}
            self.assertIs(latest_pending_screenshot(context), screenshot)
            context["messages"].append({
                "role": "capability_result", "capability": "phone.adb",
                "result": {"success": True, "status": "completed", "exit_code": 0},
            })
            self.assertIsNone(latest_pending_screenshot(context))
        context = {"messages": [{"role": "capability_result", "capability": "phone.adb", "result": screenshot}]}
        mark_screenshot_attempted(context, screenshot)
        self.assertEqual(screenshot["visual_attempts"], 1)
        compact_consumed_screenshot(context, screenshot)
        self.assertIsNone(latest_pending_screenshot(context))
        self.assertEqual(context["messages"][0]["result"]["status"], "visual_input_sent")


if __name__ == "__main__":
    unittest.main()
