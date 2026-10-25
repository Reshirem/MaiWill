"""为意志请求提供受限的本地截图读取。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SCREENSHOT_BYTES = 6 * 1024 * 1024
MAX_SCREENSHOT_SIDE = 8192
MAX_SCREENSHOT_PIXELS = 40_000_000
MAX_VISUAL_ATTEMPTS = 2
_SCREENSHOT_PATH = re.compile(r"phone/screenshots/[0-9a-f]{32}\.png\Z")


def latest_pending_screenshot(context: dict[str, Any]) -> dict[str, Any] | None:
    """只返回最近一次 phone.use 截图结果；其他手机操作会使旧图过期。"""
    messages = context.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "capability_result" or message.get("capability") != "phone.use":
            continue
        result = message.get("result")
        if (
            not isinstance(result, dict)
            or result.get("success") is not True
            or result.get("status") != "screenshot"
            or result.get("visual_input_sent") is True
        ):
            return None
        return result
    return None


def load_screenshot_png(data_dir: Path, relative_path: str) -> bytes:
    """从 MaiWill 专属截图目录读取一个小型、有效头部的 PNG。"""
    if not isinstance(relative_path, str) or not _SCREENSHOT_PATH.fullmatch(relative_path):
        raise ValueError("截图路径不符合 MaiWill 截图目录格式")

    phone_dir = data_dir / "phone"
    screenshots_dir = phone_dir / "screenshots"
    target = data_dir / Path(relative_path)
    if phone_dir.is_symlink() or screenshots_dir.is_symlink() or target.is_symlink():
        raise ValueError("截图路径不能经过符号链接")
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(screenshots_dir.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("截图不在 MaiWill 专属截图目录内或文件不存在") from exc

    size = resolved.stat().st_size
    if size < 33 or size > MAX_SCREENSHOT_BYTES:
        raise ValueError("截图文件大小超出允许范围")
    image = resolved.read_bytes()
    if len(image) < 33 or len(image) > MAX_SCREENSHOT_BYTES or not image.startswith(PNG_SIGNATURE):
        raise ValueError("截图不是有效的 PNG 文件或文件大小超限")
    if image[12:16] != b"IHDR":
        raise ValueError("PNG 截图缺少 IHDR 头")
    width = int.from_bytes(image[16:20], "big")
    height = int.from_bytes(image[20:24], "big")
    if (
        width < 1
        or height < 1
        or width > MAX_SCREENSHOT_SIDE
        or height > MAX_SCREENSHOT_SIDE
        or width * height > MAX_SCREENSHOT_PIXELS
    ):
        raise ValueError("截图分辨率超出允许范围")
    return image


def mark_screenshot_attempted(context: dict[str, Any], screenshot: dict[str, Any]) -> None:
    """记录图片已随请求发送，限制失败后自动重发次数。"""
    messages = context.get("messages")
    if not isinstance(messages, list):
        return
    path = screenshot.get("path")
    for message in reversed(messages):
        if (
            isinstance(message, dict)
            and message.get("role") == "capability_result"
            and message.get("capability") == "phone.use"
            and isinstance(message.get("result"), dict)
            and message["result"].get("path") == path
        ):
            attempts = int(message["result"].get("visual_attempts", 0))
            message["result"]["visual_attempts"] = attempts + 1
            return


def compact_consumed_screenshot(context: dict[str, Any], screenshot: dict[str, Any]) -> None:
    """决策成功后将能力结果压缩为已发送标记，去掉路径和 Base64 元数据。"""
    messages = context.get("messages")
    if not isinstance(messages, list):
        return
    path = screenshot.get("path")
    for message in reversed(messages):
        if (
            isinstance(message, dict)
            and message.get("role") == "capability_result"
            and message.get("capability") == "phone.use"
            and isinstance(message.get("result"), dict)
            and message["result"].get("path") == path
        ):
            message["result"] = {
                "success": True,
                "status": "visual_input_sent",
            }
            return


def estimate_prompt_image_tokens(image: bytes) -> int:
    """按常见高分辨率图像 patch 计数做保守上下文预算。"""
    if len(image) < 24 or image[12:16] != b"IHDR":
        return 0
    width = int.from_bytes(image[16:20], "big")
    height = int.from_bytes(image[20:24], "big")
    return max(256, ((width + 511) // 512) * ((height + 511) // 512) * 256)
