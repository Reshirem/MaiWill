"""单一逻辑意志会话的生命周期管理。"""

from __future__ import annotations

import asyncio
import time

from .agent import AgentLoop, CapabilityHandler, DecisionProvider
from .storage import Storage


class SessionRuntime:
    """保证一个插件实例最多运行一个意志监督任务。"""

    def __init__(
        self,
        storage: Storage,
        *,
        enabled: bool,
        stable_window_seconds: int = 60,
        decision_provider: DecisionProvider | None = None,
        capability_handlers: dict[str, CapabilityHandler] | None = None,
        reconnect_initial_seconds: int = 5,
        reconnect_multiplier: float = 2.0,
        reconnect_max_seconds: int = 600,
        rest_max_seconds: int = 43200,
        idle_wait_seconds: int = 3600,
        auto_start: bool = False,
    ) -> None:
        self.storage = storage
        self.enabled = enabled
        self.stable_window_seconds = stable_window_seconds
        self.decision_provider = decision_provider
        self.capability_handlers = capability_handlers or {}
        self.reconnect_initial_seconds = reconnect_initial_seconds
        self.reconnect_multiplier = reconnect_multiplier
        self.reconnect_max_seconds = reconnect_max_seconds
        self.rest_max_seconds = rest_max_seconds
        self.idle_wait_seconds = idle_wait_seconds
        self.auto_start = auto_start
        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if not self.enabled:
            self.storage.set_session_status("DISABLED")
            return
        if self.auto_start:
            session = await asyncio.to_thread(self.storage.get_session)
            if not session.get("manual_stop") and session["status"] in {"STOPPED", "DISABLED"}:
                await asyncio.to_thread(self.storage.start_session)
        if self._task is not None and not self._task.done():
            self._wake_event.set()
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="maiwill-session-supervisor")

    async def start_session(self) -> None:
        if not self.enabled:
            raise RuntimeError("MaiWill 插件已停用")
        await asyncio.to_thread(self.storage.start_session)
        await self.wake()

    async def request_stop(self) -> dict[str, object]:
        result = await asyncio.to_thread(self.storage.request_session_stop)
        await self.wake()
        return result

    async def force_stop(self) -> dict[str, object]:
        await asyncio.to_thread(self.storage.force_stop_session)
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self.storage.complete_force_stop)
        if self.enabled and (self._task is None or self._task.done()):
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name="maiwill-session-supervisor")
        return await asyncio.to_thread(self.storage.get_session)

    async def new_session(self) -> dict[str, object]:
        """Stop any in-flight work, reset the logical context, and resume autonomously."""
        if not self.enabled:
            raise RuntimeError("MaiWill 插件已停用")
        task = self._task
        self._task = None
        if task is not None and not task.done():
            self._stop_event.set()
            self._wake_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self.storage.force_stop_session)
        await asyncio.to_thread(self.storage.complete_force_stop)
        session = await asyncio.to_thread(self.storage.create_new_session)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="maiwill-session-supervisor")
        self._wake_event.set()
        return session

    async def wake(self) -> None:
        self._wake_event.set()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        self._stop_event.set()
        self._wake_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        """保持唯一监督任务，并串行运行 Agent。"""
        try:
            while not self._stop_event.is_set():
                self._wake_event.clear()
                session = await asyncio.to_thread(self.storage.get_session)
                if session["status"] in {"DISABLED", "STOPPED"}:
                    await self._wait_for_wake(3600)
                    continue
                if session["stable_since"] is not None:
                    await asyncio.to_thread(
                        self.storage.clear_reconnect_after_stable,
                        self.stable_window_seconds,
                    )
                    session = await asyncio.to_thread(self.storage.get_session)
                if session["status"] in {"ACTIVE", "STOP_REQUESTED"}:
                    if self.decision_provider is None:
                        await asyncio.to_thread(
                            self.storage.set_session_status,
                            "DISCONNECTED",
                            last_error="没有配置意志会话决策提供方。",
                        )
                    else:
                        await self._run_agent()
                    continue

                deadline: float | None = None
                if session["status"] == "RESTING":
                    # A rest interval is self-directed downtime, not a period in
                    # which external signals are ignored. User messages and other
                    # queued observations interrupt rest and return to the same
                    # logical session.
                    if await asyncio.to_thread(self.storage.get_pending_events, 1):
                        await asyncio.to_thread(self.storage.wake_rest_for_events)
                        continue
                    deadline = session["rest_until"]
                elif session["status"] == "DISCONNECTED":
                    deadline = session["next_retry_at"]
                if deadline is None:
                    await self._wait_for_wake(3600)
                    continue

                # Persisted deadlines are UTC Unix timestamps, not monotonic loop times.
                delay = max(0.0, deadline - time.time())
                if delay == 0:
                    if session["status"] == "RESTING":
                        await asyncio.to_thread(
                            self.storage.activate_due_rest,
                        )
                    else:
                        await asyncio.to_thread(
                            self.storage.activate_due_retry,
                        )
                    continue
                await self._wait_for_wake(min(delay, 3600))

        except asyncio.CancelledError:
            try:
                await asyncio.to_thread(self.storage.mark_started_actions_unknown)
            except Exception:
                # The force-stop path performs durable uncertainty marking after cancellation.
                pass
            raise

    async def _wait_for_wake(self, timeout_seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return

    async def _run_agent(self) -> None:
        assert self.decision_provider is not None
        agent = AgentLoop(
            self.storage,
            self.decision_provider,
            self.capability_handlers,
            rest_max_seconds=self.rest_max_seconds,
            idle_wait_seconds=self.idle_wait_seconds,
            stable_window_seconds=self.stable_window_seconds,
        )
        try:
            normal = await agent.run()
            await asyncio.to_thread(
                self.storage.finish_agent_run,
                normal=normal,
                retry_initial_seconds=self.reconnect_initial_seconds,
                retry_multiplier=self.reconnect_multiplier,
                retry_max_seconds=self.reconnect_max_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.storage.finish_agent_run,
                normal=False,
                retry_initial_seconds=self.reconnect_initial_seconds,
                retry_multiplier=self.reconnect_multiplier,
                retry_max_seconds=self.reconnect_max_seconds,
                last_error=f"意志 Agent 运行失败：{exc}",
            )


def resource_budget_policy() -> dict[str, object]:
    """返回体力的事后用量记账策略。"""
    return {
        "available": True,
        "mode": "post_call_usage",
        "reason": "每次调用按宿主返回的实际 usage 事后扣减；体力可透支，余额作为模型休息决策的参考。",
    }
