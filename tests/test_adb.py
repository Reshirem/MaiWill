from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maiwill.adb import DirectAdb
from maiwill.vision import load_screenshot_png


FAKE_ADB = r'''
import json, sys, time
args = sys.argv[1:]
if args == ['connect', '127.0.0.1:16384']:
    print('connected to configured device')
    sys.exit(0)
assert args[:2] == ['-s', '127.0.0.1:16384'], args
argv = args[2:]
if argv == ['exec-out', 'screencap', '-p']:
    ihdr = (320).to_bytes(4, 'big') + (240).to_bytes(4, 'big') + bytes((8, 2, 0, 0, 0))
    sys.stdout.buffer.write(b'\x89PNG\r\n\x1a\n' + (13).to_bytes(4, 'big') + b'IHDR' + ihdr + b'\0' * 4)
elif argv == ['shell', 'exit-test']:
    print('first command ran', flush=True)
    print('second command failed', file=sys.stderr, flush=True)
    sys.exit(7)
elif argv == ['shell', 'large-output']:
    sys.stdout.write('a' * 200000)
    sys.stderr.write('b' * 200000)
elif argv == ['shell', 'hang']:
    print('partial output', flush=True)
    time.sleep(60)
else:
    stdin = sys.stdin.buffer.read().decode('utf-8')
    print(json.dumps({'argv': argv, 'stdin': stdin}, ensure_ascii=False))
'''


class DirectAdbTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.executable = self.root / "fake_adb.py"
        self.executable.write_text(FAKE_ADB, encoding="utf-8")
        self.adapter = DirectAdb(str(self.executable), "127.0.0.1:16384", self.root)
        self.calls = []
        self.processes = []
        self.started = asyncio.Event()
        real_spawn = asyncio.create_subprocess_exec

        async def spawn(*argv, **kwargs):
            self.calls.append((argv, kwargs))
            self.assertEqual(argv[0], str(self.executable))
            process = await real_spawn(sys.executable, "-X", "utf8", str(self.executable), *argv[1:], **kwargs)
            self.processes.append(process)
            self.started.set()
            return process

        self.spawn_patch = patch("maiwill.adb.asyncio.create_subprocess_exec", spawn)
        self.spawn_patch.start()

    def tearDown(self) -> None:
        self.spawn_patch.stop()
        self.directory.cleanup()

    async def test_exact_argv_and_script_are_sent_once_without_implicit_actions(self) -> None:
        script = '  value="中文 \\\"引用\\\""\ninput tap 10 20\nsleep 0.2\nprintf "%s\\n" "$value"\n\n'
        argv = ["shell", "-T", "sh", "-s"]
        result = await self.adapter.execute({"argv": argv, "stdin": script})
        self.assertTrue(result["success"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(json.loads(result["stdout"]), {"argv": argv, "stdin": script})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], (str(self.executable), "-s", "127.0.0.1:16384", *argv))
        self.assertFalse((self.root / "phone").exists())

    async def test_remote_options_are_not_mistaken_for_global_device_flags(self) -> None:
        argv = ["shell", "printf", "%s", "-s", "-H", "a b", '"quoted"', ";"]
        result = await self.adapter.execute({"argv": argv})
        self.assertEqual(json.loads(result["stdout"]), {"argv": argv, "stdin": ""})
        self.assertEqual(self.calls[0][1]["stdin"], asyncio.subprocess.DEVNULL)
        self.assertNotIn("shell", self.calls[0][1])

    async def test_only_explicit_connect_uses_configured_address(self) -> None:
        result = await self.adapter.execute({"argv": ["connect"]})
        self.assertTrue(result["success"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], (str(self.executable), "connect", "127.0.0.1:16384"))

    async def test_nonzero_exit_keeps_both_streams_and_does_not_retry(self) -> None:
        result = await self.adapter.execute({"argv": ["shell", "exit-test"]})
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("first command ran", result["stdout"])
        self.assertIn("second command failed", result["stderr"])
        self.assertFalse(result["retryable"])
        self.assertEqual(len(self.calls), 1)

    async def test_large_output_is_drained_but_bounded(self) -> None:
        result = await self.adapter.execute({"argv": ["shell", "large-output"]})
        self.assertTrue(result["success"])
        self.assertEqual(len(result["stdout"]), 12000)
        self.assertEqual(len(result["stderr"]), 12000)
        self.assertTrue(result["stdout_truncated"])
        self.assertTrue(result["stderr_truncated"])

    async def test_explicit_screenshot_saves_binary_from_the_same_command(self) -> None:
        result = await self.adapter.execute({"argv": ["exec-out", "screencap", "-p"]})
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "screenshot")
        self.assertEqual(result["stdout"], "")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(load_screenshot_png(self.root, result["path"]).startswith(b"\x89PNG"))
        self.assertNotIn("image_base64", result)

    async def test_timeout_stops_local_process_and_reports_unknown_device_outcome(self) -> None:
        result = await self.adapter.execute({"argv": ["shell", "hang"], "timeout_seconds": 1})
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertFalse(result["retryable"])
        self.assertIn("partial output", result["stdout"])
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertEqual(len(self.calls), 1)

    async def test_incomplete_or_invalid_screenshot_is_not_reported_as_success(self) -> None:
        with patch("maiwill.adb.MAX_SCREENSHOT_BYTES", 16):
            oversized = await self.adapter.execute({"argv": ["exec-out", "screencap", "-p"]})
        self.assertFalse(oversized["success"])
        self.assertTrue(oversized["stdout_truncated"])
        self.executable.write_text('import sys; sys.stdout.write("invalid image")', encoding="utf-8")
        invalid = await self.adapter.execute({"argv": ["exec-out", "screencap", "-p"]})
        self.assertFalse(invalid["success"])
        self.assertEqual(invalid["status"], "observation_error")
        self.assertFalse((self.root / "phone").exists())
        self.assertEqual(len(self.calls), 2)

    async def test_not_configured_does_not_start_adb(self) -> None:
        self.adapter = DirectAdb("", "127.0.0.1:16384", self.root)
        result = await self.adapter.execute({"argv": ["get-state"]})
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(self.calls, [])

    async def test_cancellation_reaps_local_process_and_propagates(self) -> None:
        task = asyncio.create_task(self.adapter.execute({"argv": ["shell", "hang"]}))
        await asyncio.wait_for(self.started.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(self.processes[0].returncode)

    async def test_invalid_requests_never_spawn_a_process(self) -> None:
        for arguments in (
            {}, {"argv": []}, {"argv": "shell input tap 1 2"}, {"argv": [1]},
            {"argv": ["-s", "other", "shell"]}, {"argv": ["kill-server"]},
            {"argv": ["connect", "other-device"]}, {"argv": ["shell"], "stdin": 1},
            {"argv": ["shell"], "stdin": "bad\0script"}, {"argv": ["shell\0"]},
            {"argv": ["shell"], "timeout_seconds": True}, {"argv": ["shell"], "timeout_seconds": 0},
            {"argv": ["shell"], "timeout_seconds": 121}, {"argv": ["shell"], "settle_ms": 100},
            {"argv": ["get-state"], "stdin": "unexpected script"},
        ):
            with self.subTest(arguments=arguments):
                result = await self.adapter.execute(arguments)
                self.assertFalse(result["success"])
                self.assertEqual(result["status"], "invalid_arguments")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
