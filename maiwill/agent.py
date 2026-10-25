"""意志会话的可恢复决策与能力往返。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from .storage import Storage

DecisionProvider = Callable[
    [str, dict[str, Any], list[dict[str, Any]]], Awaitable[dict[str, Any]]
]
CapabilityHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class AgentLoop:
    """在一个逻辑会话中串行请求决策并执行单步能力。"""

    def __init__(
        self,
        storage: Storage,
        decision_provider: DecisionProvider,
        capability_handlers: dict[str, CapabilityHandler] | None = None,
        *,
        rest_max_seconds: int = 43200,
        stable_window_seconds: int = 60,
        idle_wait_seconds: int = 3600,
    ) -> None:
        if not 1 <= rest_max_seconds <= 43200:
            raise ValueError("rest_max_seconds 必须是 1 到 43200 秒")
        if type(idle_wait_seconds) is not int or not 1 <= idle_wait_seconds <= 43200:
            raise ValueError("idle_wait_seconds 必须是 1 到 43200 秒")
        self.storage = storage
        self.decision_provider = decision_provider
        self.capability_handlers = capability_handlers or {}
        self.rest_max_seconds = rest_max_seconds
        self.idle_wait_seconds = min(idle_wait_seconds, rest_max_seconds)
        self.stable_window_seconds = stable_window_seconds

    async def run(self) -> bool:
        """执行一轮 Agent；返回 True 表示 Agent 正常结束。"""
        session = await asyncio.to_thread(self.storage.get_session)
        session_id = session["session_id"]
        context = session.get("context") or {}
        context.setdefault("schema_version", 1)
        context.setdefault("messages", [])
        context.setdefault("turn", 0)
        await asyncio.to_thread(self.storage.mark_session_stable)

        invalid_decisions = 0
        while True:
            session = await asyncio.to_thread(self.storage.get_session)
            context["control_requests"] = {"stop_requested": bool(session.get("stop_requested", False))}
            events = await asyncio.to_thread(self.storage.get_pending_events)
            for event in events:
                if event["kind"] != "operator.message":
                    await asyncio.to_thread(
                        self.storage.append_conversation,
                        {"role": "observation", "events": [self._event_for_context(event)]},
                        entry_key="event:" + event["event_id"], event_id=event["event_id"],
                        now=event["created_at"],
                    )
            resource = await asyncio.to_thread(self.storage.resource_snapshot)
            context["resource"] = {
                "balance": round(resource["balance"], 2),
                "capacity": resource["capacity"],
                "recovery_per_hour": resource["recovery_per_hour"],
                "spent_tokens": resource["spent_tokens"],
                "unknown_usage_request_count": resource["unknown_usage_request_count"],
                "balance_note": resource["balance_note"],
            }
            try:
                decision = await self.decision_provider(session_id, context, events)
            finally:
                context.pop("control_requests", None)
            session = await asyncio.to_thread(self.storage.get_session)
            if session.get("stop_requested"):
                decision = {
                    "decision": "finish",
                    "reason": "响应用户的停止请求，结束本轮会话。",
                }
            else:
                try:
                    self._validate_decision(decision)
                except (TypeError, ValueError, KeyError) as exc:
                    if isinstance(decision, dict):
                        request_id = decision.pop("_usage_request_id", None)
                        if isinstance(request_id, str):
                            await asyncio.to_thread(self.storage.record_invalid_decision, request_id)
                    invalid_decisions += 1
                    feedback = (
                        f"上一次决策格式无效：{exc}。请从 available_capabilities 中选择完全匹配的能力名，"
                        "并按要求重新输出一个有效 JSON 决策；不要重复无效字段。"
                    )
                    await asyncio.to_thread(
                        self.storage.append_conversation,
                        {"role": "observation", "note": feedback},
                    )
                    context["messages"].append({"role": "observation", "note": feedback})
                    await asyncio.to_thread(self.storage.save_session_context, context)
                    if invalid_decisions >= 3:
                        await asyncio.to_thread(
                            self.storage.append_conversation,
                            {"role": "observation", "note": "连续 3 次决策格式无效，本轮暂停并按断连退避重试；未处理消息仍保留在队列中。"},
                        )
                        await asyncio.to_thread(self.storage.save_session_context, context)
                        return False
                    continue
            if isinstance(decision, dict):
                decision.pop("_usage_request_id", None)
            invalid_decisions = 0

            # `finish` no longer ends the user's autonomous loop. Unless a human
            # requested a stop, convert an idle finish into a durable rest interval
            # so the same logical session wakes and explores again later.
            if decision["decision"] == "finish" and not session.get("stop_requested"):
                decision = {
                    "decision": "rest",
                    "duration_seconds": self.idle_wait_seconds,
                    "reason": "本轮没有需要立即执行的工作，进入自主休息并在到期后继续探索。",
                    "reply": decision.get("reply", ""),
                }
            await asyncio.to_thread(
                self.storage.append_conversation, {**decision, "role": "assistant"}
            )
            await asyncio.to_thread(
                self.storage.clear_reconnect_after_stable,
                self.stable_window_seconds,
            )

            if events:
                context["messages"].append(
                    {
                        "role": "observation",
                        "events": [self._event_for_context(event) for event in events],
                    }
                )

            if decision["decision"] == "rest":
                duration = await asyncio.to_thread(
                    self.storage.request_rest,
                    decision["duration_seconds"],
                    decision.get("reason", ""),
                    maximum_seconds=self.rest_max_seconds,
                )
                context["messages"].append(
                    {
                        "role": "assistant",
                        "decision": "rest",
                        "duration_seconds": duration,
                        "reason": decision.get("reason", ""),
                        "reply": decision.get("reply", ""),
                    }
                )
                context["turn"] += 1
                await asyncio.to_thread(
                    self.storage.save_session_context,
                    context,
                    handled_event_ids=[event["event_id"] for event in events],
                )
                remaining = await asyncio.to_thread(self.storage.get_pending_events, 1)
                if remaining:
                    await asyncio.to_thread(self.storage.start_session)
                    continue
                return True

            if decision["decision"] == "finish" and session.get("stop_requested"):
                context["messages"].append(
                    {"role": "assistant", "decision": "finish", "reason": decision.get("reason", ""),
                     "reply": decision.get("reply", "")}
                )
                context["turn"] += 1
                await asyncio.to_thread(
                    self.storage.save_session_context,
                    context,
                    handled_event_ids=[event["event_id"] for event in events],
                )
                return True

            result = await self._execute_capability(session_id, context, decision)
            await asyncio.to_thread(
                self.storage.append_conversation,
                {"role": "capability_result", "capability": decision["capability"], "result": result},
            )
            context["messages"].append(
                {
                    "role": "assistant",
                    "decision": "use_capability",
                    "capability": decision["capability"],
                    "arguments": decision["arguments"],
                    "reason": decision.get("reason", ""),
                    "reply": decision.get("reply", ""),
                }
            )
            context["messages"].append(
                {"role": "capability_result", "capability": decision["capability"], "result": result}
            )
            context["turn"] += 1
            await asyncio.to_thread(
                self.storage.save_session_context,
                context,
                handled_event_ids=[event["event_id"] for event in events],
            )


    async def _execute_capability(
        self,
        session_id: str,
        context: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        capability = decision["capability"]
        arguments = decision["arguments"]
        if capability == "chat.proactive":
            step_payload = {"capability": capability, "arguments": arguments}
        else:
            step_payload = {"session_id": session_id, "context": context, "decision": decision}
        step_source = json.dumps(
            step_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        step_id = hashlib.sha256(step_source.encode("utf-8")).hexdigest()
        action = await asyncio.to_thread(self.storage.start_action, step_id, capability, arguments)

        if action["status"] in {"completed", "failed", "outcome_unknown"}:
            if action["status"] == "outcome_unknown" and not action["result"]:
                return {
                    "success": False,
                    "error": "能力执行结果未知；为避免重复副作用，未自动重放。",
                    "retryable": False,
                }
            return action["result"]
        if action["status"] != "started":
            return {"success": False, "error": f"未知动作状态：{action['status']}"}

        handler = self.capability_handlers.get(capability)
        if handler is None:
            result = {"success": False, "error": f"能力未注册：{capability}"}
            status = "failed"
        else:
            try:
                handler_arguments = (
                    {**arguments, "_step_id": step_id}
                    if capability == "chat.proactive"
                    else arguments
                )
                result = await handler(handler_arguments)
                if not isinstance(result, dict):
                    result = {"success": True, "result": result}
                status = "completed" if result.get("success", True) else "failed"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = {
                    "success": False,
                    "error": f"能力执行结果未知，不能安全自动重放：{exc}",
                    "retryable": False,
                }
                status = "outcome_unknown"

        if status == "completed":
            result = {
                **result,
                "evidence_type": "action",
                "evidence_ref": action["action_id"],
            }
        await asyncio.to_thread(self.storage.finish_action, step_id, status, result)
        return result

    @staticmethod
    def _validate_decision(decision: Any) -> None:
        if not isinstance(decision, dict):
            raise ValueError("意志决策必须是对象")
        if decision.get("_format_error"):
            raise ValueError(str(decision["_format_error"])[:500])
        kind = decision.get("decision")
        if kind == "rest":
            if type(decision.get("duration_seconds")) is not int or decision["duration_seconds"] <= 0:
                raise ValueError("rest.duration_seconds 必须是正整数")
        elif kind == "finish":
            pass
        elif kind == "use_capability":
            capability = decision.get("capability")
            if not isinstance(capability, str) or not capability.strip() or len(capability) > 128:
                raise ValueError("能力名必须是 1 到 128 个字符")
            if not isinstance(decision.get("arguments"), dict):
                raise ValueError("能力 arguments 必须是对象")
        else:
            raise ValueError(f"未知意志决策：{kind}")
        reason = decision.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 1000:
            raise ValueError("reason 必须是最多 1000 个字符的字符串")
        reply = decision.get("reply", "")
        if not isinstance(reply, str) or len(reply) > 4000:
            raise ValueError("reply 必须是最多 4000 个字符的字符串")

    @staticmethod
    def _event_for_context(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": event["event_id"],
            "kind": event["kind"],
            "created_at": event["created_at"],
            "priority": event["priority"],
            "payload": event["payload"],
        }
