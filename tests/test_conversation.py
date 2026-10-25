from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from maiwill.agent import AgentLoop
from maiwill.runtime import SessionRuntime
from maiwill.storage import Storage


class ConversationStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "maiwill.sqlite3"
        self.storage = Storage(self.path)
        self.storage.initialize_session(enabled=True)
        self.storage.sync_resource_config(1000, 0, start_at_capacity=True)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_concurrent_retries_commit_one_event_and_message(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda _: self.storage.submit_intervention("  先记下观察，再休息。  ", "a" * 32), range(8)
            ))
        self.assertEqual(sum(item["created"] for item in results), 1)
        self.assertEqual(len(self.storage.get_pending_events()), 1)
        conversation = self.storage.get_conversation()
        self.assertEqual(len(conversation["messages"]), 1)
        self.assertEqual(conversation["messages"][0]["content"], "先记下观察，再休息。")
        self.assertEqual(conversation["session"]["status"], "STOPPED")
        self.assertEqual(conversation["pending_interventions"], 1)
        with self.assertRaises(ValueError):
            self.storage.submit_intervention("不同的内容", "a" * 32)

    def test_invalid_messages_do_not_create_partial_records(self) -> None:
        for content, message_id in [(" \n", "a" * 32), ("字" * 4001, "a" * 32), (None, "a" * 32), ("你好", "bad/id"), ("你好", 123)]:
            with self.subTest(content=str(content)[:20]), self.assertRaises(ValueError):
                self.storage.submit_intervention(content, message_id)
        self.assertEqual(self.storage.get_pending_events(), [])
        self.assertEqual(self.storage.get_conversation()["messages"], [])

    def test_enqueue_rolls_back_when_transcript_write_fails(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TRIGGER reject_conversation BEFORE INSERT ON will_conversation BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.submit_intervention("不可只写入一半", "a" * 32)
        self.assertEqual(self.storage.get_pending_events(), [])

    def test_context_compaction_and_reload_preserve_transcript_and_delivery(self) -> None:
        result = self.storage.submit_intervention("请记下发现", "a" * 32)
        self.storage.save_session_context({"messages": []}, handled_event_ids=[result["event_id"]])
        reopened = Storage(self.path)
        conversation = reopened.get_conversation()
        self.assertEqual(conversation["messages"][0]["status"], "handled")
        self.assertEqual(conversation["pending_interventions"], 0)
        self.assertEqual(reopened.submit_intervention("请记下发现", "a" * 32)["status"], "handled")
        self.assertEqual(len(reopened.get_conversation()["messages"]), 1)
        self.assertNotIn("context", conversation["session"])

    def test_current_context_summary_reports_size_without_returning_messages(self) -> None:
        saved = {"messages": [{"role": "observation", "note": "你好"}]}
        self.storage.save_session_context(saved)

        summary = self.storage.get_current_context_summary()

        self.assertEqual(summary["session_id"], self.storage.get_session()["session_id"])
        self.assertEqual(summary["message_count"], 1)
        self.assertEqual(summary["serialized_bytes"], len(json.dumps(saved, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
        self.assertGreater(summary["estimated_tokens"], 0)
        self.assertNotIn("messages", summary)
        self.assertNotIn("你好", json.dumps(summary, ensure_ascii=False))

    def test_new_session_resets_context_preserves_old_transcript_and_pending_events(self) -> None:
        first = self.storage.get_session()["session_id"]
        self.storage.save_session_context({"messages": [{"role": "assistant", "reason": "旧上下文"}]})
        self.storage.append_conversation({"role": "assistant", "decision": "finish", "reason": "旧会话记录"})
        pending = self.storage.submit_intervention("新会话也请处理这条消息", "new-session-pending-001")
        new = self.storage.create_new_session(now=1234)

        self.assertNotEqual(new["session_id"], first)
        self.assertEqual(new["status"], "ACTIVE")
        self.assertEqual(new["context"], {})
        self.assertEqual(new["manual_stop"], 0)
        self.assertEqual(self.storage.get_pending_events()[0]["event_id"], pending["event_id"])
        old_history = self.storage.get_conversation(session_id=first)
        self.assertEqual([item["content"] for item in old_history["messages"]], ["旧会话记录"])
        current = self.storage.get_conversation()
        self.assertEqual(current["conversation_session_id"], new["session_id"])
        created = next(item for item in current["messages"] if item["role"] == "system")
        self.assertEqual(created["content"], "已新建意识会话；此前的模型上下文已清空，旧会话记录仍保留。")
        self.assertEqual(current["messages"][0]["role"], "user")
        self.assertEqual(current["messages"][0]["status"], "queued")
        with self.assertRaises(ValueError):
            self.storage.get_conversation(session_id="missing")

    def test_new_session_clears_context_but_keeps_exploration_goal(self) -> None:
        interest = self.storage.record_interest(
            "海洋潮汐", "我想继续了解", "curiosity", [], external_id="goal-session-interest", now=1200,
        )
        goal = self.storage.create_exploration_goal(
            "了解潮汐", "潮汐", "整理潮汐知识", "interest", interest["external_id"], "兴趣事件", now=1201,
        )
        old_session = self.storage.get_session()["session_id"]
        new_session = self.storage.create_new_session(now=1202)

        self.assertNotEqual(new_session["session_id"], old_session)
        self.assertEqual(new_session["context"], {})
        self.assertEqual(self.storage.get_exploration_goals()[0]["goal_id"], goal["goal_id"])

    def test_upgrade_imports_retained_history_once_without_fabricating_time(self) -> None:
        self.storage.save_session_context({"messages": [{"role": "assistant", "decision": "finish", "reason": "旧会话记录"}]})
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE will_conversation")
            connection.execute("DROP TABLE will_conversation_migration")
        upgraded = Storage(self.path)
        self.assertEqual(upgraded.get_conversation()["messages"][0]["content"], "旧会话记录")
        self.assertIsNone(upgraded.get_conversation()["messages"][0]["created_at"])
        self.assertEqual(len(Storage(self.path).get_conversation()["messages"]), 1)
        self.assertEqual(upgraded.get_session()["context"]["messages"][0]["reason"], "旧会话记录")

    def test_cursor_pagination_is_complete_under_utf8_response_budget(self) -> None:
        for index in range(55):
            self.storage.append_conversation({
                "role": "capability_result", "capability": "test.result",
                "result": {"summary": f"结果 {index}", "content": "汉" * 20000},
            })
        seen, before = [], 0
        while True:
            page = self.storage.get_conversation(before=before, limit=50)
            self.assertLess(len(json.dumps(page, ensure_ascii=False).encode("utf-8")), 512 * 1024)
            batch = [item["sequence"] for item in page["messages"]]
            self.assertEqual(batch, sorted(batch))
            seen.extend(batch)
            if not page["has_more"]:
                break
            self.assertLess(page["next_before"], before or 10**9)
            before = page["next_before"]
        self.assertEqual(len(seen), 55)
        self.assertEqual(len(set(seen)), 55)
        for arguments in ({"before": -1}, {"before": True}, {"limit": 0}, {"limit": 51}):
            with self.assertRaises(ValueError):
                self.storage.get_conversation(**arguments)


class ConversationRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.directory.name) / "maiwill.sqlite3")
        self.storage.initialize_session(enabled=True)
        self.storage.sync_resource_config(1000, 0, start_at_capacity=True)

    def tearDown(self) -> None:
        self.directory.cleanup()

    async def test_messages_arriving_during_tool_run_join_next_serial_decision(self) -> None:
        tool_started, release_tool = asyncio.Event(), asyncio.Event()
        calls, contexts = [], []

        async def provider(session_id, context, events):
            calls.append(events)
            contexts.append(json.loads(json.dumps(context)))
            if len(calls) == 1:
                return {"decision": "use_capability", "capability": "test.tool", "arguments": {}, "reason": "正在观察"}
            return {"decision": "finish", "reason": "按用户请求结束", "reply": "好的，已经记下了。"}

        async def tool(arguments):
            tool_started.set()
            await release_tool.wait()
            return {"success": True, "summary": "观察完成"}

        self.storage.start_session()
        agent = AgentLoop(self.storage, provider, {"test.tool": tool})
        task = asyncio.create_task(agent.run())
        try:
            await asyncio.wait_for(tool_started.wait(), 2)
            self.storage.submit_intervention("把发现记下来。", "a" * 32)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.storage.get_conversation()["pending_interventions"], 1)
            release_tool.set()
            await asyncio.wait_for(task, 3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0]["kind"], "operator.message")
        self.assertEqual(calls[1][0]["payload"]["content"], "把发现记下来。")
        self.assertEqual(contexts[1]["messages"][-1]["role"], "capability_result")
        transcript = self.storage.get_conversation()
        self.assertEqual([item["role"] for item in transcript["messages"]], ["assistant", "user", "tool", "assistant"])
        self.assertEqual(transcript["messages"][1]["status"], "handled")
        self.assertEqual(transcript["messages"][-1]["content"], "好的，已经记下了。")
        self.assertEqual(self.storage.get_session()["context"]["messages"][-1]["reply"], "好的，已经记下了。")

    async def test_stopped_message_waits_until_explicit_start(self) -> None:
        calls, called = [], asyncio.Event()

        async def provider(session_id, context, events):
            calls.append(events)
            called.set()
            return {"decision": "rest", "duration_seconds": 3600, "reply": "收到。"}

        runtime = SessionRuntime(self.storage, enabled=True, decision_provider=provider)
        await runtime.start()
        try:
            self.storage.submit_intervention("停止期间的消息", "a" * 32)
            await asyncio.sleep(.05)
            self.assertEqual(calls, [])
            await runtime.start_session()
            await asyncio.wait_for(called.wait(), 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0]["payload"]["content"], "停止期间的消息")
            for _ in range(100):
                if self.storage.get_session()["status"] == "RESTING":
                    break
                await asyncio.sleep(.01)
            self.assertEqual(self.storage.get_session()["status"], "RESTING")
        finally:
            await runtime.stop()

    async def test_resting_message_wakes_same_session_without_waiting_for_deadline(self) -> None:
        self.storage.start_session()
        self.storage.request_rest(3600, "麦麦选择长休息")
        self.storage.finish_agent_run(
            normal=True,
            retry_initial_seconds=5,
            retry_multiplier=2,
            retry_max_seconds=600,
        )
        original_session_id = self.storage.get_session()["session_id"]
        seen = asyncio.Event()
        received = []

        async def provider(session_id, context, events):
            received.extend(events)
            seen.set()
            return {"decision": "rest", "duration_seconds": 3600}

        runtime = SessionRuntime(self.storage, enabled=True, decision_provider=provider)
        await runtime.start()
        try:
            await asyncio.sleep(.03)
            self.storage.submit_intervention("请现在帮我看看", "wake-rest-test-0001")
            await runtime.wake()
            await asyncio.wait_for(seen.wait(), 1)
            self.assertEqual(self.storage.get_session()["session_id"], original_session_id)
            self.assertEqual(received[0]["payload"]["content"], "请现在帮我看看")
        finally:
            await runtime.stop()

    async def test_failed_model_call_keeps_intervention_for_reload_and_retry(self) -> None:
        self.storage.submit_intervention("稍后继续研究", "a" * 32)

        async def failing_provider(session_id, context, events):
            raise RuntimeError("模拟断连")

        with self.assertRaises(RuntimeError):
            await AgentLoop(self.storage, failing_provider).run()
        reopened = Storage(self.storage.database_path)
        self.assertEqual(reopened.get_conversation()["messages"][0]["status"], "queued")

        async def provider(session_id, context, events):
            self.assertEqual(events[0]["payload"]["content"], "稍后继续研究")
            return {"decision": "rest", "duration_seconds": 20, "reply": "我会稍后继续。"}

        await AgentLoop(reopened, provider).run()
        transcript = reopened.get_conversation()["messages"]
        self.assertEqual(transcript[0]["status"], "handled")
        self.assertEqual(transcript[1]["content"], "我会稍后继续。")
        self.assertEqual(len([item for item in transcript if item["role"] == "user"]), 1)

    async def test_invalid_reply_is_retried_without_acknowledging_message(self) -> None:
        self.storage.submit_intervention("请回复", "a" * 32)
        calls = []

        async def provider(session_id, context, events):
            calls.append(events)
            if len(calls) == 1:
                return {"decision": "use_capability", "capability": "", "arguments": {}}
            return {"decision": "rest", "duration_seconds": 20, "reply": "收到，我来看看。"}

        await AgentLoop(self.storage, provider).run()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.storage.get_conversation()["pending_interventions"], 0)
        context_messages = self.storage.get_session()["context"]["messages"]
        self.assertEqual(context_messages[-2]["events"][0]["kind"], "operator.message")
        self.assertIn("决策格式无效", context_messages[0]["note"])

    async def test_three_invalid_decisions_keep_message_queued_and_enter_backoff(self) -> None:
        self.storage.submit_intervention("试试手机", "invalid-phone-message-01")
        self.storage.start_session()
        calls = []
        third_decision = asyncio.Event()

        async def provider(session_id, context, events):
            calls.append(events)
            if len(calls) == 3:
                third_decision.set()
            return {"decision": "use_capability", "capability": "", "arguments": {}}

        runtime = SessionRuntime(
            self.storage,
            enabled=True,
            decision_provider=provider,
            reconnect_initial_seconds=1,
            reconnect_max_seconds=2,
        )
        await runtime.start()
        try:
            await asyncio.wait_for(third_decision.wait(), 2)
            for _ in range(100):
                if self.storage.get_session()["status"] == "DISCONNECTED":
                    break
                await asyncio.sleep(.01)

            snapshot = self.storage.get_status_snapshot()
            self.assertEqual(len(calls), 3)
            self.assertEqual(snapshot["session"]["status"], "DISCONNECTED")
            self.assertIsNotNone(snapshot["session"]["next_retry_at"])
            self.assertIsNone(snapshot["session"]["rest_requested_seconds"])
            self.assertEqual(self.storage.get_conversation()["pending_interventions"], 1)
        finally:
            await runtime.stop()


if __name__ == "__main__":
    unittest.main()
