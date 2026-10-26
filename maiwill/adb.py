"""直接调用配置设备的 ADB，不经过手机手势封装或隐式操作。"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from typing import Any

from .phone import AdbPhone, _HOST_COMMANDS
from .vision import MAX_SCREENSHOT_BYTES


_TEXT_OUTPUT_BYTES = 12000
_SCREENSHOT_ARGV = ["exec-out", "screencap", "-p"]


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """持续排空管道避免子进程阻塞，只保留有界输出。"""
    data = bytearray()
    truncated = False
    while chunk := await stream.read(65536):
        remaining = limit - len(data)
        data.extend(chunk[:remaining])
        truncated = truncated or len(chunk) > remaining
    return bytes(data), truncated


class DirectAdb(AdbPhone):
    """仅复用设备配置和截图落盘，不调用 use/_run 或自动连接。"""

    @staticmethod
    def _validate_request(arguments: dict[str, Any]) -> tuple[list[str], bytes | None, int]:
        if not isinstance(arguments, dict) or set(arguments) - {"argv", "stdin", "timeout_seconds"}:
            raise ValueError("phone.adb 只接受 argv、stdin 和 timeout_seconds")
        argv = arguments.get("argv")
        if (
            not isinstance(argv, list) or not 1 <= len(argv) <= 128
            or any(not isinstance(value, str) or "\0" in value or len(value) > 32768 for value in argv)
            or sum(len(value) for value in argv) > 65536
        ):
            raise ValueError("argv 必须是 1 到 128 个字符串，总长度最多 65536 字符，不能包含 NUL")
        command = argv[0]
        # ADB parses global selectors before its command. Subsequent arguments
        # belong to that command: do not mistake remote sh -s, logcat -s, etc.
        # for a request to select another device or rewrite remote shell text.
        if not re.fullmatch(r"[a-z][a-z0-9-]*", command):
            raise ValueError("argv 从 ADB 子命令开始，不包含 adb 路径或 -s 等全局选项")
        if command == "connect":
            if argv != ["connect"]:
                raise ValueError("连接配置设备请只传 argv=[\"connect\"]，设备地址由配置提供")
        elif command in _HOST_COMMANDS:
            raise ValueError("不能通过 phone.adb 改选设备或控制 ADB 主机")
        stdin = arguments.get("stdin")
        if stdin is not None and (
            not isinstance(stdin, str) or "\0" in stdin or len(stdin) > 65536
        ):
            raise ValueError("stdin 必须是最多 65536 字符且不含 NUL 的脚本文本")
        if stdin is not None and command not in {"shell", "exec-in", "exec-out"}:
            raise ValueError("stdin 仅用于 shell、exec-in 或 exec-out")
        timeout = arguments.get("timeout_seconds", 30)
        if type(timeout) is not int or not 1 <= timeout <= 120:
            raise ValueError("timeout_seconds 必须是 1 到 120 的整数")
        return list(argv), stdin.encode("utf-8") if stdin is not None else None, timeout

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            argv, stdin, timeout = self._validate_request(arguments)
        except (ValueError, UnicodeError) as exc:
            return {"success": False, "status": "invalid_arguments", "error": str(exc)}
        if not self.configured:
            return {"success": False, "status": "not_configured", "error": "请先配置 ADB 程序路径"}
        command = (
            [self.adb_path, "connect", self.address]
            if argv == ["connect"] else [self.adb_path, "-s", self.address, *argv]
        )
        is_screenshot = argv == _SCREENSHOT_ARGV and stdin is None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            return {"success": False, "status": "not_started", "error": f"无法启动 ADB：{exc}"}

        async def write_stdin() -> None:
            if stdin is None:
                return
            try:
                process.stdin.write(stdin)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        output = asyncio.create_task(_read_bounded(
            process.stdout, MAX_SCREENSHOT_BYTES if is_screenshot else _TEXT_OUTPUT_BYTES,
        ))
        errors = asyncio.create_task(_read_bounded(process.stderr, _TEXT_OUTPUT_BYTES))
        writer = asyncio.create_task(write_stdin())
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                await asyncio.gather(output, errors, writer, return_exceptions=True)
                raise
            timed_out = True
        stdout_result, stderr_result, _ = await asyncio.gather(output, errors, writer)
        stdout, stdout_truncated = stdout_result
        stderr, stderr_truncated = stderr_result
        result: dict[str, Any] = {
            "success": not timed_out and process.returncode == 0,
            "status": "outcome_unknown" if timed_out else "completed" if process.returncode == 0 else "error",
            "exit_code": process.returncode,
            "stdout": "" if is_screenshot else stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        if timed_out:
            result.update({
                "error": f"ADB 超过 {timeout} 秒，已终止本机调用；设备端脚本可能已部分执行，请先观察当前状态",
                "retryable": False,
            })
        elif process.returncode:
            result.update({
                "error": f"ADB 退出码 {process.returncode}；请查看 stdout/stderr，脚本前面的操作可能已执行",
                "retryable": False,
            })
        elif is_screenshot:
            # This saves the bytes from this explicit command. It never launches
            # an extra screenshot command or changes the requested input sequence.
            try:
                if stdout_truncated:
                    raise ValueError("截图超出 6 MiB 限制，未保存不完整 PNG")
                result.update(await self._store_screenshot(stdout))
                result["status"] = "screenshot"
            except (OSError, ValueError, RuntimeError) as exc:
                result.update({"success": False, "status": "observation_error", "error": str(exc)})
        return result
