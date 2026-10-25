"""意志决策的输出约定、有限包装兼容与可定位校验。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Collection
from typing import Any


DECISION_OUTPUT_INSTRUCTION = (
    "输出必须符合 decision_schema，只输出一个完整 JSON 决策对象。"
    "调用能力时必须包含 decision=use_capability、capability 和 arguments；"
    "capability 必须逐字匹配本轮 available_capabilities 的 name，arguments 必须是对象，"
    "手机 action 等能力参数只能放在 arguments 内。"
    "休息时必须包含 decision=rest 和正整数 duration_seconds；停止时使用 decision=finish。"
    "reason 最多 1000 字符，reply 最多 4000 字符，二者都是可选字符串。"
    "不要输出数组、多个对象、Markdown、解释前后缀或额外字段；"
    "字符串中的换行、双引号和反斜杠必须按 JSON 转义。"
)


class DecisionFormatError(ValueError):
    """携带不含模型原文的结构化诊断，供重试和会话详情使用。"""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.diagnostics = {"error_code": code, **details}


def decision_schema(available_capabilities: Collection[str]) -> dict[str, Any]:
    """作为提示数据发送；不依赖宿主尚未公开的 response_format 参数。"""
    common = {
        "reason": {"type": "string", "maxLength": 1000},
        "reply": {"type": "string", "maxLength": 4000},
    }
    branches = []
    for kind, fields, required in (
        (
            "use_capability",
            {
                "capability": {"type": "string", "enum": sorted(available_capabilities)},
                "arguments": {"type": "object"},
            },
            ["capability", "arguments"],
        ),
        ("rest", {"duration_seconds": {"type": "integer", "minimum": 1}}, ["duration_seconds"]),
        ("finish", {}, []),
    ):
        branches.append({
            "type": "object",
            "properties": {"decision": {"const": kind}, **fields, **common},
            "required": ["decision", *required],
            "additionalProperties": False,
        })
    return {"oneOf": branches}


def validate_decision(
    decision: Any, *, available_capabilities: Collection[str] | None = None,
) -> None:
    """验证决策字段；具体能力参数仍由各适配器校验。"""
    if not isinstance(decision, dict):
        raise DecisionFormatError("not_object", "意志决策必须是单个 JSON 对象，不能是数组或其他值")
    kind = decision.get("decision")
    allowed = {"decision", "reason", "reply"}
    if kind == "rest":
        allowed.add("duration_seconds")
        if type(decision.get("duration_seconds")) is not int or decision["duration_seconds"] <= 0:
            raise DecisionFormatError("invalid_duration", "rest.duration_seconds 必须是正整数")
    elif kind == "finish":
        pass
    elif kind == "use_capability":
        allowed.update(("capability", "arguments"))
        capability = decision.get("capability")
        if not isinstance(capability, str) or not capability.strip() or len(capability) > 128:
            raise DecisionFormatError("invalid_capability", "use_capability.capability 必须是 1 到 128 个字符的能力名")
        if available_capabilities is not None and capability not in available_capabilities:
            raise DecisionFormatError(
                "unavailable_capability",
                f"capability {json.dumps(capability, ensure_ascii=False)} 不在本轮 available_capabilities.name 中；请逐字选取可用能力名",
            )
        if not isinstance(decision.get("arguments"), dict):
            raise DecisionFormatError("invalid_arguments", "use_capability.arguments 必须是 JSON 对象，不能是字符串或数组")
    else:
        raise DecisionFormatError("invalid_kind", "decision 必须是 use_capability、rest 或 finish")
    for field, maximum in (("reason", 1000), ("reply", 4000)):
        value = decision.get(field, "")
        if not isinstance(value, str) or len(value) > maximum:
            raise DecisionFormatError("invalid_" + field, f"{field} 必须是最多 {maximum} 个字符的字符串")
    unexpected = [str(key)[:80] for key in decision if key not in allowed]
    if unexpected:
        raise DecisionFormatError(
            "unexpected_fields",
            "当前 decision 不允许这些顶层字段：" + json.dumps(unexpected[:5], ensure_ascii=False),
        )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DecisionFormatError("duplicate_key", "JSON 对象含重复字段；每个字段只能出现一次")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise DecisionFormatError("invalid_number", "JSON 不允许 NaN 或 Infinity，请使用有限数值")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise DecisionFormatError("invalid_number", "JSON 数值超出有限浮点数范围")
    return number


def parse_decision_response(
    response: Any, *, available_capabilities: Collection[str] | None = None,
) -> dict[str, Any]:
    """只去掉 BOM 和包住全文的 JSON 代码围栏，不猜测或修补决策内容。"""
    diagnostics: dict[str, Any] = {"response_type": type(response).__name__}
    try:
        if not isinstance(response, str):
            raise DecisionFormatError("missing_text", "MaiBot LLM 响应缺少字符串类型的 response 文本")
        diagnostics.update({
            "response_chars": len(response),
            "response_sha256": hashlib.sha256(response.encode("utf-8", errors="surrogatepass")).hexdigest(),
        })
        text = response.strip()
        wrappers = []
        if text.startswith("\ufeff"):
            text = text[1:].strip()
            wrappers.append("bom")
        fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", text, re.IGNORECASE | re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
            wrappers.append("json_fence")
        diagnostics["normalized_wrappers"] = wrappers
        if not text:
            raise DecisionFormatError("empty_response", "模型返回了空文本；需要一个完整 JSON 决策对象")
        try:
            decision = json.loads(
                text, object_pairs_hook=_unique_object, parse_constant=_reject_constant, parse_float=_finite_float,
            )
        except json.JSONDecodeError as exc:
            raise DecisionFormatError(
                "invalid_json",
                f"JSON 语法错误（第 {exc.lineno} 行，第 {exc.colno} 列）：{exc.msg}；"
                "请检查引号、逗号、转义和对象是否完整，正文只能包含一个决策对象",
                json_error=exc.msg, line=exc.lineno, column=exc.colno, position=exc.pos,
            ) from None
        except (RecursionError, ValueError) as exc:
            if isinstance(exc, DecisionFormatError):
                raise
            raise DecisionFormatError("json_limit", "JSON 嵌套或数值长度超出解析限制，请简化为单步决策") from None
        validate_decision(decision, available_capabilities=available_capabilities)
        return decision
    except DecisionFormatError as exc:
        exc.diagnostics.update(diagnostics)
        raise
