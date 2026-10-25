from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from maiwill.agent import AgentLoop
from maiwill.runtime import SessionRuntime
from maiwill.storage import Storage


class SessionRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name) / "maiwill.sqlite3")
        self.storage.sync_resource_config(1000, 100, start_at_capacity=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def wait_until(self, predicate, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("runtime did not reach expected state before timeout")

    async def test_enabled_runtime_stays_stopped_without_autostart(self) -> None:
        self.storage.initialize_session(enabled=True, auto_start=False)
        runtime = SessionRuntime(self.storage, enabled=True)

        await runtime.start()
        await asyncio.sleep(0.03)
        await runtime.stop()

        self.assertEqual(self.storage.get_session()["status"], "STOPPED")
        self.assertEqual(self.storage.get_status_snapshot()["queued_event_count"], 0)
        self.assertIsNone(runtime._task)

    async def test_start_button_wakes_only_after_explicit_request_and_stop_is_graceful(self) -> None:
        self.storage.initialize_session(enabled=True, auto_start=False)
        provider_calls = []
        stop_seen = asyncio.Event()

        async def provider(session_id, context, events):
            provider_calls.append(session_id)
            if context.get("control_requests", {}).get("stop_requested"):
                stop_seen.set()
                return {"decision": "use_capability", "capability": "test.echo", "arguments": {}}
            return {"decision": "rest", "duration_seconds": 10}

        async def echo(arguments):
            raise AssertionError("stop request must prevent starting a new capability")

        runtime = SessionRuntime(
            self.storage, enabled=True, decision_provider=provider,
            capability_handlers={"test.echo": echo},
        )
        await runtime.start()
        await asyncio.sleep(0.03)
        self.assertEqual(provider_calls, [])
        await runtime.start_session()
        await runtime.request_stop()
        await asyncio.wait_for(stop_seen.wait(), timeout=1)
        await self.wait_until(lambda: self.storage.get_session()["status"] == "STOPPED")
        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(self.storage.get_session()["run_requested"], 0)
        await runtime.stop()

    async def test_normal_finish_enters_self_directed_rest_cycle(self) -> None:
        self.storage.initialize_session(enabled=True)
        self.storage.start_session()

        async def provider(session_id, context, events):
            return {"decision": "finish", "reason": "当前没有待办"}

        runtime = SessionRuntime(self.storage, enabled=True, decision_provider=provider)
        await runtime.start()
        await runtime.start_session()
        await self.wait_until(lambda: self.storage.get_session()["status"] == "RESTING")
        self.assertGreater(self.storage.get_session()["rest_until"], time.time())
        await runtime.stop()

    async def test_force_stop_cancels_inflight_decision_and_persists_stopped(self) -> None:
        self.storage.initialize_session(enabled=True)
        await asyncio.to_thread(self.storage.start_session)
        request_started = asyncio.Event()
        cancelled = asyncio.Event()

        async def provider(session_id, context, events):
            request_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        runtime = SessionRuntime(self.storage, enabled=True, decision_provider=provider)
        await runtime.start()
        await asyncio.wait_for(request_started.wait(), timeout=1)
        stopped = await runtime.force_stop()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(stopped["status"], "STOPPED")
        self.assertEqual(self.storage.get_session()["run_requested"], 0)
        await runtime.stop()

    async def test_new_session_cancels_old_loop_and_starts_with_empty_context(self) -> None:
        started, cancelled = asyncio.Event(), asyncio.Event()
        calls = []

        async def provider(session_id, context, events):
            calls.append((session_id, list(context.get("messages", []))))
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.storage.initialize_session(enabled=True)
        self.storage.start_session()
        self.storage.save_session_context({"messages": [{"role": "assistant", "reason": "旧上下文"}]})
        runtime = SessionRuntime(self.storage, enabled=True, decision_provider=provider)
        await runtime.start()
        try:
            await asyncio.wait_for(started.wait(), 1)
            old_session_id = calls[0][0]
            new = await runtime.new_session()
            await self.wait_until(lambda: len(calls) >= 2)
            self.assertTrue(cancelled.is_set())
            self.assertNotEqual(new["session_id"], old_session_id)
            self.assertEqual(calls[1][0], new["session_id"])
            self.assertEqual(calls[1][1], [])
            self.assertEqual(self.storage.get_session()["status"], "ACTIVE")
        finally:
            await runtime.stop()

    async def test_restart_of_explicitly_started_session_retries_and_finish_enters_rest(self) -> None:
        self.storage.initialize_session(enabled=True, auto_start=False)
        self.storage.start_session(now=time.time() - 100)
        self.storage.finish_agent_run(
            normal=False, retry_initial_seconds=5, retry_multiplier=2, retry_max_seconds=600,
            last_error="simulated crash", now=time.time() - 100,
        )
        provider_calls = []

        async def provider(session_id, context, events):
            provider_calls.append(session_id)
            return {"decision": "finish"}

        runtime = SessionRuntime(self.storage, enabled=True, decision_provider=provider)
        await runtime.start()
        await self.wait_until(lambda: len(provider_calls) == 1)
        await self.wait_until(lambda: self.storage.get_session()["status"] == "RESTING")
        self.assertEqual(len(provider_calls), 1)
        await runtime.stop()

    async def test_due_rest_wakes_and_runs_original_session(self) -> None:
        now = time.time()
        self.storage.initialize_session(enabled=True, auto_start=False, now=now)
        self.storage.start_session(now=now)
        self.storage.request_rest(1, "runtime test", now=now - 10)
        self.storage.finish_agent_run(
            normal=True,
            retry_initial_seconds=5,
            retry_multiplier=2,
            retry_max_seconds=600,
            now=now - 10,
        )
        provider_calls = []

        async def provider(session_id, context, events):
            provider_calls.append((session_id, events))
            return {"decision": "rest", "duration_seconds": 10}

        runtime = SessionRuntime(self.storage, enabled=True, decision_provider=provider)

        await runtime.start()
        await self.wait_until(lambda: bool(provider_calls))
        await self.wait_until(lambda: self.storage.get_session()["status"] == "RESTING")
        await asyncio.sleep(0.03)
        await runtime.stop()

        snapshot = self.storage.get_status_snapshot()
        self.assertEqual(len(provider_calls), 1)
        self.assertLess(provider_calls[0][1][0]["payload"].get("scheduled_for", 0), now)
        self.assertEqual(snapshot["queued_event_count"], 0)

    async def test_disabled_runtime_does_not_start_supervisor_task(self) -> None:
        self.storage.initialize_session(enabled=False)
        runtime = SessionRuntime(self.storage, enabled=False)

        await runtime.start()

        self.assertEqual(self.storage.get_session()["status"], "DISABLED")
        self.assertIsNone(runtime._task)

    async def test_initialized_session_stays_stopped_without_autostart(self) -> None:
        self.storage.initialize_session(enabled=True)
        provider_calls = []

        async def provider(session_id, context, events):
            provider_calls.append(session_id)
            return {"decision": "finish"}

        runtime = SessionRuntime(self.storage, enabled=True, decision_provider=provider)
        await runtime.start()
        await asyncio.sleep(0.03)
        await runtime.stop()

        self.assertEqual(provider_calls, [])
        self.assertEqual(self.storage.get_session()["status"], "STOPPED")

    async def test_runtime_autostart_keeps_one_autonomous_rest_loop(self) -> None:
        self.storage.initialize_session(enabled=True, auto_start=False)
        calls = []

        async def provider(session_id, context, events):
            calls.append(session_id)
            return {"decision": "rest", "duration_seconds": 30, "reason": "自主休息"}

        runtime = SessionRuntime(
            self.storage, enabled=True, auto_start=True, decision_provider=provider,
            reconnect_initial_seconds=1, reconnect_multiplier=2, reconnect_max_seconds=2,
        )
        await runtime.start()
        await self.wait_until(lambda: self.storage.get_session()["status"] == "RESTING")
        await runtime.stop()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], self.storage.get_session()["session_id"])
        self.assertGreater(self.storage.get_session()["rest_until"], time.time())

    async def test_fake_agent_executes_capability_and_enters_rest(self) -> None:
        self.storage.initialize_session(enabled=True, auto_start=False)
        self.storage.start_session()
        self.storage.enqueue_event(
            "interest_recorded",
            {"topic": "海洋潮汐"},
            dedupe_key="interest:agent-test",
        )
        decisions = [
            {
                "decision": "use_capability",
                "capability": "test.echo",
                "arguments": {"value": "观察结果"},
                "reason": "测试能力往返",
            },
            {"decision": "rest", "duration_seconds": 30, "reason": "测试休息"},
        ]
        provider_inputs = []
        handler_calls = []

        async def provider(session_id, context, events):
            provider_inputs.append((session_id, context.copy(), events))
            return decisions.pop(0)

        async def echo(arguments):
            handler_calls.append(arguments)
            return {"success": True, "value": arguments["value"]}

        runtime = SessionRuntime(
            self.storage,
            enabled=True,
            decision_provider=provider,
            capability_handlers={"test.echo": echo},
            rest_max_seconds=60,
        )
        await runtime.start()
        await self.wait_until(lambda: self.storage.get_session()["status"] == "RESTING")
        await runtime.stop()

        snapshot = self.storage.get_status_snapshot()
        self.assertEqual(len(provider_inputs), 2)
        self.assertEqual(provider_inputs[0][2][0]["payload"]["topic"], "海洋潮汐")
        self.assertEqual(handler_calls, [{"value": "观察结果"}])
        self.assertEqual(snapshot["queued_event_count"], 0)
        self.assertEqual(snapshot["session"]["rest_requested_seconds"], None)
        self.assertAlmostEqual(snapshot["session"]["rest_until"] - snapshot["server_time"], 30, delta=1)
        self.assertEqual(snapshot["recent_actions"][0]["status"], "completed")
        self.assertEqual(snapshot["session"]["context"]["messages"][-1]["decision"], "rest")

    async def test_agent_session_has_no_fixed_32_step_action_limit(self) -> None:
        self.storage.initialize_session(enabled=True)
        calls = 0

        async def provider(session_id, context, events):
            nonlocal calls
            calls += 1
            if calls <= 40:
                return {
                    "decision": "use_capability",
                    "capability": "test.echo",
                    "arguments": {"step": calls},
                }
            return {"decision": "rest", "duration_seconds": 30}

        async def echo(arguments):
            return {"success": True, "step": arguments["step"]}

        agent = AgentLoop(self.storage, provider, {"test.echo": echo})
        self.assertTrue(await agent.run())

        snapshot = self.storage.get_status_snapshot()
        self.assertEqual(calls, 41)
        self.assertEqual(len(snapshot["recent_actions"]), 10)
        self.assertEqual(snapshot["recent_actions"][0]["arguments"], {"step": 40})
        self.assertEqual(snapshot["session"]["status"], "REST_REQUESTED")

    async def test_identical_proactive_intent_is_enqueued_only_once(self) -> None:
        self.storage.initialize_session(enabled=True)
        proactive = {
            "decision": "use_capability",
            "capability": "chat.proactive",
            "arguments": {
                "interest_id": "interest-1",
                "stream_id": "stream-1",
                "intent": "分享刚整理的观察",
                "reason": "确实值得分享",
                "priority": "normal",
            },
        }
        decisions = [proactive, proactive, {"decision": "rest", "duration_seconds": 30}]
        calls = []

        async def provider(session_id, context, events):
            return decisions.pop(0)

        async def proactive_handler(arguments):
            calls.append(arguments)
            return {"success": True, "task_id": "task-1"}

        agent = AgentLoop(self.storage, provider, {"chat.proactive": proactive_handler})
        await agent.run()

        self.assertEqual(len(calls), 1)
        action = self.storage.get_status_snapshot()["recent_actions"][0]
        self.assertEqual(action["status"], "completed")

    async def test_memory_search_result_returns_to_the_same_logical_session(self) -> None:
        self.storage.initialize_session(enabled=True)
        self.storage.start_session()
        self.storage.enqueue_event(
            "interest_recorded",
            {"topic": "海洋潮汐"},
            dedupe_key="interest:memory-search-test",
        )
        decisions = [
            {
                "decision": "use_capability",
                "capability": "memory.search",
                "arguments": {"query": "潮汐", "limit": 3},
            },
            {"decision": "rest", "duration_seconds": 30},
        ]
        provider_inputs = []
        search_calls = []

        async def provider(session_id, context, events):
            provider_inputs.append((session_id, {**context, "messages": list(context["messages"])}))
            return decisions.pop(0)

        async def search_memory(arguments):
            search_calls.append(arguments)
            return {"success": True, "content": "与当前主题有关的共享记忆"}

        runtime = SessionRuntime(
            self.storage,
            enabled=True,
            decision_provider=provider,
            capability_handlers={"memory.search": search_memory},
        )
        await runtime.start()
        await self.wait_until(lambda: self.storage.get_session()["status"] == "RESTING")
        await runtime.stop()

        self.assertEqual(len(provider_inputs), 2)
        self.assertEqual(provider_inputs[0][0], provider_inputs[1][0])
        self.assertEqual(search_calls, [{"query": "潮汐", "limit": 3}])
        messages = provider_inputs[1][1]["messages"]
        self.assertEqual(messages[-1]["role"], "capability_result")
        self.assertEqual(messages[-1]["capability"], "memory.search")
        self.assertEqual(
            messages[-1]["result"]["content"],
            "与当前主题有关的共享记忆",
        )

    async def test_diary_write_result_returns_to_the_same_logical_session(self) -> None:
        self.storage.initialize_session(enabled=True)
        self.storage.start_session()
        decisions = [
            {
                "decision": "use_capability",
                "capability": "diary.write",
                "arguments": {"title": "一次观察", "content": "潮水正在上涨。"},
            },
            {"decision": "rest", "duration_seconds": 30},
        ]
        provider_inputs = []
        diary_calls = []

        async def provider(session_id, context, events):
            provider_inputs.append((session_id, {**context, "messages": list(context["messages"])}))
            return decisions.pop(0)

        async def write_diary(arguments):
            diary_calls.append(arguments)
            return {"success": True, "path": "diary/observation.md"}

        runtime = SessionRuntime(
            self.storage,
            enabled=True,
            decision_provider=provider,
            capability_handlers={"diary.write": write_diary},
        )
        await runtime.start()
        await self.wait_until(lambda: self.storage.get_session()["status"] == "RESTING")
        await runtime.stop()

        self.assertEqual(len(provider_inputs), 2)
        self.assertEqual(provider_inputs[0][0], provider_inputs[1][0])
        self.assertEqual(
            diary_calls,
            [{"title": "一次观察", "content": "潮水正在上涨。"}],
        )
        messages = provider_inputs[1][1]["messages"]
        self.assertEqual(messages[-1]["role"], "capability_result")
        self.assertEqual(messages[-1]["capability"], "diary.write")
        self.assertEqual(messages[-1]["result"]["evidence_type"], "action")
        action = self.storage.get_recent_successful_actions(limit=1)[0]
        self.assertEqual(messages[-1]["result"]["evidence_ref"], action["action_id"])

    async def test_completed_capability_is_not_replayed_if_context_save_failed(self) -> None:
        self.storage.initialize_session(enabled=True)
        self.storage.set_session_status("ACTIVE")
        self.storage.enqueue_event("interest_recorded", {"topic": "潮汐"}, dedupe_key="interest:replay")
        decisions = [
            {"decision": "use_capability", "capability": "test.echo", "arguments": {"value": 1}},
            {"decision": "rest", "duration_seconds": 10},
        ]
        handler_calls = []

        async def provider(session_id, context, events):
            return decisions.pop(0)

        async def echo(arguments):
            handler_calls.append(arguments)
            return {"success": True, "value": arguments["value"]}

        agent = AgentLoop(self.storage, provider, {"test.echo": echo})
        original_save = self.storage.save_session_context
        self.storage.save_session_context = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disk"))
        with self.assertRaisesRegex(RuntimeError, "disk"):
            await agent.run()
        self.storage.save_session_context = original_save

        self.assertEqual(self.storage.get_status_snapshot()["queued_event_count"], 1)
        self.assertEqual(self.storage.get_status_snapshot()["recent_actions"][0]["status"], "completed")

        self.assertTrue(await agent.run())
        self.assertEqual(handler_calls, [{"value": 1}])
        self.assertEqual(self.storage.get_status_snapshot()["queued_event_count"], 0)

    async def test_capability_exception_is_recorded_as_outcome_unknown(self) -> None:
        self.storage.initialize_session(enabled=True)
        self.storage.set_session_status("ACTIVE")
        decisions = [
            {"decision": "use_capability", "capability": "test.effect", "arguments": {"value": 1}},
            {"decision": "rest", "duration_seconds": 10},
        ]
        handler_calls = []

        async def provider(session_id, context, events):
            return decisions.pop(0)

        async def uncertain_effect(arguments):
            handler_calls.append(arguments)
            raise RuntimeError("connection dropped after send")

        runtime = SessionRuntime(
            self.storage,
            enabled=True,
            decision_provider=provider,
            capability_handlers={"test.effect": uncertain_effect},
        )
        await runtime.start()
        await self.wait_until(lambda: self.storage.get_session()["status"] == "RESTING")
        await runtime.stop()

        snapshot = self.storage.get_status_snapshot()
        self.assertEqual(handler_calls, [{"value": 1}])
        self.assertEqual(snapshot["recent_actions"][0]["status"], "outcome_unknown")
        result = snapshot["session"]["context"]["messages"][-2]["result"]
        self.assertFalse(result["retryable"])
        self.assertIn("不能安全自动重放", result["error"])

    async def test_unload_cancels_inflight_action_without_replaying_it(self) -> None:
        self.storage.initialize_session(enabled=True)
        self.storage.set_session_status("ACTIVE")
        action = {"decision": "use_capability", "capability": "test.wait", "arguments": {"value": 1}}
        decisions = [action, action, {"decision": "rest", "duration_seconds": 10}]
        action_started = asyncio.Event()
        handler_calls = []

        async def provider(session_id, context, events):
            return decisions.pop(0)

        async def wait_action(arguments):
            handler_calls.append(arguments)
            action_started.set()
            await asyncio.Event().wait()

        first_runtime = SessionRuntime(
            self.storage,
            enabled=True,
            decision_provider=provider,
            capability_handlers={"test.wait": wait_action},
        )
        await first_runtime.start()
        await asyncio.wait_for(action_started.wait(), timeout=1)
        await first_runtime.stop()
        self.assertEqual(self.storage.get_status_snapshot()["recent_actions"][0]["status"], "outcome_unknown")

        second_runtime = SessionRuntime(
            self.storage,
            enabled=True,
            decision_provider=provider,
            capability_handlers={"test.wait": wait_action},
        )
        await second_runtime.start()
        await self.wait_until(lambda: self.storage.get_session()["status"] == "RESTING")
        await second_runtime.stop()

        snapshot = self.storage.get_status_snapshot()
        self.assertEqual(handler_calls, [{"value": 1}])
        result = snapshot["session"]["context"]["messages"][-2]["result"]
        self.assertFalse(result["retryable"])
        self.assertIn("运行器取消时", result["error"])

    async def test_enabled_runtime_runs_provider_without_budget_flag(self) -> None:
        self.storage.initialize_session(enabled=True)
        self.storage.start_session()
        provider_calls = []

        async def provider(session_id, context, events):
            provider_calls.append(session_id)
            return {"decision": "rest", "duration_seconds": 10}

        runtime = SessionRuntime(self.storage, enabled=True, decision_provider=provider)
        await runtime.start()
        await self.wait_until(lambda: self.storage.get_session()["status"] == "RESTING")
        await runtime.stop()

        self.assertEqual(len(provider_calls), 1)

    async def test_decision_provider_sees_negative_balance_and_can_choose_rest(self) -> None:
        now = time.time()
        # This assertion concerns post-call debt, not elapsed-time recovery.
        self.storage.sync_resource_config(1000, 0, start_at_capacity=True, now=now)
        self.storage.initialize_session(enabled=True, auto_start=False, now=now)
        self.storage.start_session(now=now)
        self.storage.begin_usage_request("initial-overdraw", now=now)
        self.storage.settle_usage(
            "initial-overdraw",
            prompt_tokens=1200,
            completion_tokens=800,
            total_tokens=2000,
            success=True,
            now=now,
        )
        provider_inputs = []

        async def provider(session_id, context, events):
            provider_inputs.append(context["resource"].copy())
            return {"decision": "rest", "duration_seconds": 30, "reason": "体力透支"}

        runtime = SessionRuntime(self.storage, enabled=True, decision_provider=provider)
        await runtime.start()
        await self.wait_until(lambda: self.storage.get_session()["status"] == "RESTING")
        await runtime.stop()

        self.assertEqual(provider_inputs[0]["balance"], -1000)
        self.assertIn("实际 usage", provider_inputs[0]["balance_note"])
        self.assertTrue(self.storage.get_session()["rest_until"] > now)


if __name__ == "__main__":
    unittest.main()
