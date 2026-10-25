"""长意志会话的确定性上下文管理。"""

from __future__ import annotations

import json
import unicodedata
from typing import Any


def estimate_text_tokens(serialized: str) -> int:
    """以 CJK 字符约一 token、其他字符约四字符一 token 粗估文本长度。"""
    cjk_chars = sum(
        1
        for char in serialized
        if unicodedata.east_asian_width(char) in {"W", "F"}
    )
    other_chars = len(serialized) - cjk_chars
    return max(1, cjk_chars + (other_chars + 3) // 4)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """粗估 JSON 消息形状的 token 数。"""
    serialized = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return estimate_text_tokens(serialized)


def compress_context_if_needed(
    context: dict[str, Any],
    *,
    prompt_tokens: int | None,
    limit_tokens: int,
    threshold: float,
) -> int:
    """超出实测阈值时裁剪最早完整历史单元，返回裁剪的消息数。

    本函数不生成语义摘要。assistant 能力调用与对应结果作为不可拆分单元；
    不完整/孤立的能力记录保留，以免破坏崩溃恢复所需的信息。
    """
    if prompt_tokens is None or prompt_tokens < limit_tokens * threshold:
        return 0

    raw_messages = context.get("messages")
    if not isinstance(raw_messages, list):
        return 0
    if any(not isinstance(message, dict) for message in raw_messages):
        return 0
    messages = list(raw_messages)
    if not messages:
        return 0

    marker: dict[str, Any] | None = None
    if messages and messages[0].get("role") == "history_compacted":
        marker = dict(messages.pop(0))

    units: list[tuple[list[dict[str, Any]], bool]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role == "assistant" and message.get("decision") == "use_capability":
            if index + 1 < len(messages):
                result = messages[index + 1]
                if (
                    result.get("role") == "capability_result"
                    and result.get("capability") == message.get("capability")
                ):
                    keep_screenshot = (
                        result.get("capability") == "phone.use"
                        and isinstance(result.get("result"), dict)
                        and result["result"].get("status") == "screenshot"
                        and result["result"].get("visual_input_sent") is not True
                    )
                    units.append(([message, result], keep_screenshot))
                    index += 2
                    continue
            units.append(([message], True))
        elif role == "capability_result":
            units.append(([message], True))
        else:
            units.append(([message], False))
        index += 1

    target_tokens = max(1, int(limit_tokens / 2))
    retained = list(units)

    def retained_tokens() -> int:
        return sum(estimate_message_tokens(message) for unit, _ in retained for message in unit)

    discarded = 0
    while retained_tokens() > target_tokens:
        oldest_removable = next(
            (i for i, (_, pinned) in enumerate(retained) if not pinned),
            None,
        )
        if oldest_removable is None:
            break
        removed, _ = retained.pop(oldest_removable)
        discarded += len(removed)

    if discarded == 0:
        if marker is not None:
            context["messages"] = [marker] + messages
        return 0

    previous_discarded = int((marker or {}).get("discarded_message_count", 0))
    marker = {
        "role": "history_compacted",
        "discarded_message_count": previous_discarded + discarded,
        "note": "较早的会话消息已确定性裁剪；此标记不包含语义摘要。",
    }
    context["messages"] = [marker] + [message for unit, _ in retained for message in unit]
    return discarded
