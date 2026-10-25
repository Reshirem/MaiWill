"""面向插件会话页的有界、纯文本记录；与模型上下文分开保存。"""

from __future__ import annotations

import json
from typing import Any


MAX_MESSAGE_CHARS = 4000


def bounded_text(value: str, max_bytes: int = 16000) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "\n（展示内容已截断）"


def conversation_record(message: dict[str, Any]) -> dict[str, str]:
    """只展示决策说明、回复和执行结果，不要求模型输出内部推理。"""
    role = message.get("role")
    details = ""
    if role == "user":
        label, content = "你", message["content"]
    elif role == "assistant":
        kind = message.get("decision")
        label = {"rest": "麦麦 · 休息", "finish": "麦麦 · 本轮结束"}.get(kind, "麦麦 · 行动")
        content = message.get("reply") or message.get("reason") or "已作出本轮决定。"
        if message.get("reply") and message.get("reason"):
            details = "决策说明：" + message["reason"] + "\n"
        if kind == "use_capability":
            details += str(message.get("capability", "")) + "\n" + json.dumps(
                message.get("arguments", {}), ensure_ascii=False, indent=2
            )
        elif kind == "rest":
            details += f"请求休息 {message.get('duration_seconds', 0)} 秒"
    elif role == "capability_result":
        role = "tool"
        result = message.get("result", {})
        label = "工具 · " + str(message.get("capability", ""))
        content = str(result.get("summary") or result.get("error") or (
            "执行完成" if result.get("success", True) else "执行失败"
        )) if isinstance(result, dict) else str(result)
        details = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        role, label = "system", "会话观察"
        content = message.get("note") or "新的事件已加入本轮观察。"
        details = json.dumps(message, ensure_ascii=False, indent=2)
    return {
        "role": role,
        "label": label,
        "content": bounded_text(str(content)),
        "details": bounded_text(details, 12000),
    }
