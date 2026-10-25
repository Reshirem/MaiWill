from __future__ import annotations

import tempfile
import importlib.util
import sys
from types import ModuleType
import unittest
from pathlib import Path

from maiwill.storage import Storage


if "maibot_sdk" not in sys.modules:
    # The workspace interpreter has Pydantic but not the host-only SDK. Config
    # model tests need only its Pydantic base-class and Field re-export.
    from pydantic import BaseModel, Field

    sdk_stub = ModuleType("maibot_sdk")
    sdk_stub.Field = Field
    sdk_stub.PluginConfigBase = BaseModel
    sys.modules["maibot_sdk"] = sdk_stub


class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp_dir.name) / "maiwill.sqlite3")
        self.storage.initialize_session(enabled=True, auto_start=True, now=1000)
        self.storage.sync_resource_config(1000, 100, start_at_capacity=True, now=1000)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resource_recovers_up_to_capacity_and_settles_once(self) -> None:
        snapshot = self.storage.resource_snapshot(now=2800)
        self.assertEqual(snapshot["balance"], 1000)
        self.assertEqual(snapshot["balance_percent"], 100)
        self.assertEqual(snapshot["recovery_per_hour"], 100)

        self.storage.begin_usage_request("req-1", now=2800)
        settled = self.storage.settle_usage(
            "req-1",
            prompt_tokens=120,
            completion_tokens=80,
            total_tokens=200,
            success=True,
            now=2801,
        )
        replayed = self.storage.settle_usage(
            "req-1",
            prompt_tokens=120,
            completion_tokens=80,
            total_tokens=200,
            success=True,
            now=2802,
        )
        self.assertTrue(settled)
        self.assertFalse(replayed)
        self.assertEqual(self.storage.resource_snapshot(now=2801)["balance"], 800)

    def test_usage_can_overdraw_balance_and_recovery_retains_negative_balance(self) -> None:
        self.storage.begin_usage_request("req-overdraw", now=1000)
        self.storage.settle_usage(
            "req-overdraw", prompt_tokens=900, completion_tokens=300,
            total_tokens=1200, success=True, now=1000,
        )
        self.assertEqual(self.storage.resource_snapshot(now=1000)["balance"], -200)
        self.assertEqual(self.storage.resource_snapshot(now=1000)["balance_percent"], -20)
        self.assertAlmostEqual(self.storage.resource_snapshot(now=4600)["balance"], -100)

    def test_interest_outbox_is_durable_and_idempotent_with_external_id(self) -> None:
        first = self.storage.record_interest(
            "海洋潮汐",
            "我想了解潮汐如何影响海岸生态",
            "curiosity",
            ["memory-1"],
            source_stream_id="stream-1",
            external_id="interest-1",
            now=1000,
        )
        repeated = self.storage.record_interest(
            "海洋潮汐",
            "我想了解潮汐如何影响海岸生态",
            "curiosity",
            ["memory-1"],
            source_stream_id="stream-1",
            external_id="interest-1",
            now=1001,
        )
        self.assertTrue(first["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(first["status"], "pending_host_ingest")
        self.assertEqual(first["source_stream_id"], "stream-1")
        self.assertEqual(self.storage.get_pending_interests()[0]["external_id"], "interest-1")
        event_payload = self.storage.get_pending_events()[0]["payload"]
        self.assertEqual(event_payload["reason"], "我想了解潮汐如何影响海岸生态")
        self.assertEqual(event_payload["related_refs"], ["memory-1"])
        self.assertEqual(event_payload["source_stream_id"], "stream-1")
        self.assertEqual(self.storage.get_status_snapshot(now=1001)["queued_event_count"], 1)

    def test_recent_interests_remain_available_after_outbox_sync(self) -> None:
        interest = self.storage.record_interest(
            "海洋潮汐", "继续了解", "curiosity", [], source_stream_id="stream-1",
            external_id="interest-retained", now=1000,
        )
        self.assertTrue(self.storage.mark_interest_synced(interest["external_id"], now=1001))

        self.assertEqual(self.storage.get_pending_interests(), [])
        recent = self.storage.get_recent_interests(now=1002)
        self.assertEqual([item["external_id"] for item in recent], ["interest-retained"])
        self.assertEqual(recent[0]["status"], "synced")
        self.assertEqual(self.storage.get_recent_interests(now=1000 + 31 * 24 * 60 * 60), [])

    def test_learning_candidates_are_stable_local_and_have_explicit_workflow_states(self) -> None:
        first = self.storage.create_learning_candidate(
            "mai_will.phone",
            "潮汐观察",
            "近岸潮位随月球引潮力呈周期变化。",
            ["phone", "self_learning", "phone"],
            origin_session_id="session-1",
            source_ref="observation-7",
            confidence=0.83,
            now=1000,
        )
        repeated = self.storage.create_learning_candidate(
            "mai_will.phone",
            "潮汐观察",
            "近岸潮位随月球引潮力呈周期变化。",
            ["self_learning", "phone"],
            origin_session_id="session-2",
            source_ref="observation-7",
            confidence=0.83,
            now=1001,
        )
        self.assertTrue(first["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(first["external_id"], repeated["external_id"])
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["metadata"]["owner_scope"], "bot_self")
        self.assertEqual(first["metadata"]["confidence"], 0.83)
        self.assertEqual(first["tags"], ["phone", "self_learning"])
        self.assertEqual(self.storage.get_status_snapshot(now=1001)["open_learning_candidate_count"], 1)

        researching = self.storage.update_learning_candidate_status(
            first["external_id"], "researching", now=1002
        )
        self.assertEqual(researching["status"], "researching")
        resolved = self.storage.update_learning_candidate_status(
            first["external_id"], "resolved", note="已整理；等待共享记忆接口", now=1003
        )
        self.assertEqual(resolved["note"], "已整理；等待共享记忆接口")
        self.assertEqual(self.storage.get_status_snapshot(now=1003)["open_learning_candidate_count"], 0)
        self.assertEqual(
            self.storage.get_status_snapshot(now=1003)["learning_candidate_counts"],
            {
                "pending": 0,
                "researching": 0,
                "resolved": 1,
                "deferred": 0,
                "dismissed": 0,
            },
        )
        self.assertEqual(self.storage.get_learning_candidates(status="resolved")[0]["external_id"], first["external_id"])
        self.assertTrue(self.storage.delete_learning_candidate(first["external_id"]))
        self.assertFalse(self.storage.delete_learning_candidate(first["external_id"]))

        with self.assertRaises(ValueError):
            self.storage.create_learning_candidate(
                "chat_summary", "x", "y", [], origin_session_id="session-1"
            )
        with self.assertRaises(ValueError):
            self.storage.update_learning_candidate_status(first["external_id"], "synced")
        with self.assertRaises(ValueError):
            self.storage.get_learning_candidates(status="synced")
        with self.assertRaises(ValueError):
            self.storage.create_learning_candidate(
                "mai_will.knowledge", None, "content", [], origin_session_id="session-1"
            )
        with self.assertRaises(ValueError):
            self.storage.create_learning_candidate(
                "mai_will.knowledge",
                "title",
                "content",
                [],
                origin_session_id="session-1",
                confidence=2,
            )

    def test_exploration_goal_uses_durable_evidence_tracks_progress_and_cools_down_repeats(self) -> None:
        interest = self.storage.record_interest(
            "海洋潮汐", "我想弄清潮汐和月相关系", "curiosity", [],
            source_stream_id="stream-1", external_id="interest-tides", now=1000,
        )
        goal = self.storage.create_exploration_goal(
            "理解潮汐与月相", "潮汐与月相", "整理月相影响潮位的机制",
            "interest", interest["external_id"], "由明确记录的兴趣产生", now=1001,
        )
        self.assertEqual(goal["status"], "active")
        self.assertEqual(goal["progress"][0]["action"], "created")

        with self.assertRaisesRegex(ValueError, "已有进行中的探索目标"):
            self.storage.create_exploration_goal(
                "第二个", "第二主题", "目标", "interest", interest["external_id"], "依据", now=1002,
            )
        with self.assertRaisesRegex(ValueError, "必须提供可核验"):
            self.storage.update_exploration_goal(goal["goal_id"], "progress", "已经研究", now=1002)

        action = self.storage.start_action("research-step", "memory.search", {"query": "潮汐"}, now=1002)
        self.storage.finish_action(
            "research-step", "completed", {"success": True, "summary": "读取到月相相关资料"}, now=1003,
        )
        progressed = self.storage.update_exploration_goal(
            goal["goal_id"], "progress", "已读取与月相相关的资料", next_step="比较大潮和小潮周期",
            evidence_type="action", evidence_ref=action["action_id"], now=1004,
        )
        self.assertEqual(progressed["progress_summary"], "已读取与月相相关的资料")
        self.assertEqual(progressed["next_step"], "比较大潮和小潮周期")
        self.assertEqual(progressed["progress"][-1]["evidence_ref"], action["action_id"])
        with self.assertRaisesRegex(ValueError, "已经用于"):
            self.storage.update_exploration_goal(
                goal["goal_id"], "progress", "重复引用", evidence_type="action",
                evidence_ref=action["action_id"], now=1005,
            )

        completion_action = self.storage.start_action("completion-step", "diary.write", {"title": "潮汐总结"}, now=1005)
        self.storage.finish_action(
            "completion-step", "completed", {"success": True, "summary": "写入潮汐学习总结"}, now=1006,
        )
        completed = self.storage.update_exploration_goal(
            goal["goal_id"], "complete", "已整理潮汐与月相的关系", evidence_type="action",
            evidence_ref=completion_action["action_id"], now=1007,
        )
        self.assertEqual(completed["status"], "completed")
        with self.assertRaisesRegex(ValueError, "冷却期"):
            self.storage.create_exploration_goal(
                "重复研究", "潮汐与月相", "再研究一次", "interest", interest["external_id"], "依据", now=1008,
            )
        reloaded = Storage(self.storage.database_path)
        self.assertEqual(reloaded.get_exploration_goals()[0]["goal_id"], goal["goal_id"])
        with self.assertRaisesRegex(ValueError, "冷却期"):
            reloaded.create_exploration_goal(
                "重复研究", "潮汐 与 月相", "再研究一次", "interest", interest["external_id"], "依据", now=1008,
            )

    def test_exploration_goal_survives_new_logical_session_and_resume_rules(self) -> None:
        candidate = self.storage.create_learning_candidate(
            "mai_will.knowledge", "月相", "月相周期约一个月。", ["astronomy"],
            origin_session_id="session-before-reset", source_ref="source-1", now=1000,
        )
        goal = self.storage.create_exploration_goal(
            "了解月相", "月相", "整理月相周期", "learning_candidate", candidate["external_id"],
            "由已有候选产生", next_step="查找可信资料", now=1001,
        )
        self.storage.create_new_session(now=1002)
        reopened = Storage(self.storage.database_path)
        self.assertEqual(reopened.get_exploration_goals()[0]["goal_id"], goal["goal_id"])

        deferred = reopened.update_exploration_goal(
            goal["goal_id"], "defer", "暂时没有合适的来源", now=1003,
        )
        self.assertEqual(deferred["status"], "deferred")
        second = reopened.create_exploration_goal(
            "学习潮汐", "潮汐", "整理潮汐基础", "learning_candidate", candidate["external_id"],
            "相关候选", now=1004,
        )
        with self.assertRaisesRegex(ValueError, "已有进行中的探索目标"):
            reopened.update_exploration_goal(goal["goal_id"], "resume", "继续月相", now=1005)
        reopened.update_exploration_goal(second["goal_id"], "cancel", "结束此主题", now=1006)
        resumed = reopened.update_exploration_goal(goal["goal_id"], "resume", "继续整理月相", now=1007)
        self.assertEqual(resumed["status"], "active")

    def test_exploration_goal_rejects_nonexistent_and_unconfirmed_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "证据引用不存在"):
            self.storage.create_exploration_goal(
                "目标", "主题", "目的", "interest", "missing-interest", "没有依据", now=1000,
            )
        interest = self.storage.record_interest(
            "主题", "原因", "curiosity", [], external_id="interest-evidence", now=1000,
        )
        goal = self.storage.create_exploration_goal(
            "目标", "主题", "目的", "interest", interest["external_id"], "有依据", now=1001,
        )
        action = self.storage.start_action("unknown-step", "phone.use", {"action": "tap"}, now=1002)
        self.storage.finish_action("unknown-step", "outcome_unknown", {"success": False}, now=1003)
        with self.assertRaisesRegex(ValueError, "证据引用不存在"):
            self.storage.update_exploration_goal(
                goal["goal_id"], "progress", "已操作手机", evidence_type="action",
                evidence_ref=action["action_id"], now=1004,
            )

    def test_exploration_goal_replan_does_not_reset_stagnation_and_timeline_is_ordered(self) -> None:
        interest = self.storage.record_interest(
            "北极光", "想了解极光成因", "curiosity", [], external_id="aurora-interest", now=1000,
        )
        goal = self.storage.create_exploration_goal(
            "了解极光", "北极光", "整理极光形成机制", "interest", interest["external_id"],
            "记录的兴趣", next_step="查找可用资料", now=1001,
        )
        created_at = goal["last_progress_at"]
        self.assertFalse(self.storage.get_exploration_goals(now=created_at + 24 * 60 * 60 - 1)[0]["is_stalled"])
        self.assertTrue(self.storage.get_exploration_goals(now=created_at + 24 * 60 * 60)[0]["is_stalled"])

        replanned = self.storage.update_exploration_goal(
            goal["goal_id"], "replan", "现有候选不足以解释形成机制", next_step="先用手机读取科普资料",
            now=created_at + 24 * 60 * 60 + 1,
        )
        self.assertEqual(replanned["last_progress_at"], created_at)
        self.assertTrue(replanned["is_stalled"])
        self.assertEqual(replanned["progress_summary"], goal["progress_summary"])
        self.assertEqual(replanned["next_step"], "先用手机读取科普资料")

        action = self.storage.start_action("aurora-search", "memory.search", {"query": "极光成因"}, now=created_at + 90000)
        self.storage.finish_action("aurora-search", "completed", {"success": True, "content": "太阳风与磁层相互作用"}, now=created_at + 90001)
        progressed = self.storage.update_exploration_goal(
            goal["goal_id"], "progress", "资料指出极光与太阳风和磁层相互作用有关", next_step="整理成通俗解释",
            evidence_type="action", evidence_ref=action["action_id"], now=created_at + 90002,
        )
        self.assertFalse(progressed["is_stalled"])
        self.assertEqual(progressed["last_progress_at"], created_at + 90002)
        timeline = self.storage.get_exploration_timeline(limit=10)
        self.assertEqual([item["action"] for item in timeline[:3]], ["progress", "replanned", "created"])

    def test_upgrade_migrates_v022_goal_tables_without_losing_timeline(self) -> None:
        interest = self.storage.record_interest(
            "星云", "我想理解星云", "curiosity", [], external_id="nebula-interest", now=1000,
        )
        goal = self.storage.create_exploration_goal(
            "了解星云", "星云", "整理星云知识", "interest", interest["external_id"],
            "持久兴趣", now=1001,
        )
        with self.storage._connect() as connection:
            connection.execute("DROP INDEX IF EXISTS idx_exploration_progress_goal")
            connection.execute("ALTER TABLE exploration_goal_progress RENAME TO progress_v022_source")
            connection.execute(
                """CREATE TABLE exploration_goal_progress (
                    progress_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id TEXT NOT NULL REFERENCES exploration_goals(goal_id) ON DELETE CASCADE,
                    action TEXT NOT NULL CHECK (action IN ('created', 'progress', 'completed', 'deferred', 'resumed', 'cancelled')),
                    note TEXT NOT NULL, evidence_type TEXT NOT NULL DEFAULT '',
                    evidence_ref TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
                )"""
            )
            connection.execute(
                """INSERT INTO exploration_goal_progress
                   SELECT progress_id, goal_id, action, note, evidence_type, evidence_ref, created_at
                   FROM progress_v022_source"""
            )
            connection.execute("DROP TABLE progress_v022_source")
            connection.execute("DROP INDEX IF EXISTS idx_exploration_progress_goal")
            connection.execute("ALTER TABLE exploration_goals DROP COLUMN last_progress_at")
            connection.commit()

        migrated = Storage(self.storage.database_path)
        timeline = migrated.get_exploration_timeline(limit=10)
        self.assertEqual(timeline[0]["action"], "created")
        goal_after = migrated.get_exploration_goals(now=1001)[0]
        self.assertEqual(goal_after["goal_id"], goal["goal_id"])
        self.assertEqual(goal_after["last_progress_at"], goal["created_at"])
        self.assertFalse(goal_after["is_stalled"])

    def test_event_deduplication_and_interrupted_action_recovery(self) -> None:
        created = self.storage.enqueue_event(
            "timer",
            {"name": "rest_deadline"},
            dedupe_key="rest:1",
            now=1000,
        )
        duplicate = self.storage.enqueue_event(
            "timer",
            {"name": "rest_deadline"},
            dedupe_key="rest:1",
            now=1001,
        )
        self.assertTrue(created)
        self.assertFalse(duplicate)

        first = self.storage.start_action("step-1", "phone.use", {"action": "observe"}, now=1000)
        repeated = self.storage.start_action("step-1", "phone.use", {"action": "observe"}, now=1001)
        self.assertEqual(repeated["action_id"], first["action_id"])
        self.storage.initialize_session(enabled=True, now=1100)
        self.assertEqual(self.storage.get_status_snapshot(now=1100)["recent_actions"][0]["status"], "outcome_unknown")

    def test_action_step_replay_rejects_different_capability_or_arguments(self) -> None:
        self.storage.start_action("step-1", "phone.use", {"action": "observe"}, now=1000)
        with self.assertRaises(ValueError):
            self.storage.start_action("step-1", "diary.write", {"action": "observe"}, now=1001)
        with self.assertRaises(ValueError):
            self.storage.start_action("step-1", "phone.use", {"action": "tap"}, now=1002)

    def test_rest_starts_only_after_normal_agent_completion(self) -> None:
        accepted = self.storage.request_rest(90000, "需要休息", maximum_seconds=43200, now=1000)
        self.assertEqual(accepted, 43200)
        resting = self.storage.finish_agent_run(
            normal=True,
            retry_initial_seconds=5,
            retry_multiplier=2,
            retry_max_seconds=600,
            now=1100,
        )
        self.assertEqual(resting["status"], "RESTING")
        self.assertEqual(resting["rest_until"], 44300)

        self.storage.request_rest(60, "再试", now=2000)
        disconnected = self.storage.finish_agent_run(
            normal=False,
            retry_initial_seconds=5,
            retry_multiplier=2,
            retry_max_seconds=600,
            now=2010,
        )
        self.assertEqual(disconnected["status"], "DISCONNECTED")
        self.assertIsNone(disconnected["rest_until"])
        self.assertEqual(disconnected["next_retry_at"], 2015)

    def test_manual_restore_fills_pool_without_budget_reservations(self) -> None:
        self.storage.begin_usage_request("req-3", now=1000)
        restored = self.storage.restore_resource_capacity(now=1100)
        self.assertEqual(restored["balance"], 1000)
        self.assertEqual(restored["reserved_tokens"], 0)

    def test_restart_keeps_unsettled_usage_as_unknown_and_allows_later_requests(self) -> None:
        self.storage.begin_usage_request("req-unknown", now=1000)

        self.storage.initialize_session(enabled=True, now=1100)

        snapshot = self.storage.resource_snapshot(now=1100)
        self.assertEqual(snapshot["balance"], 1000)
        self.assertEqual(snapshot["reserved_tokens"], 0)
        self.assertEqual(snapshot["unknown_reserved_tokens"], 0)
        self.assertEqual(snapshot["unknown_usage_request_count"], 1)
        self.storage.begin_usage_request("later-request", now=1100)
        self.assertTrue(self.storage.settle_usage(
            "req-unknown", prompt_tokens=100, completion_tokens=100,
            total_tokens=200, success=True, now=1101,
        ))
        settled = self.storage.resource_snapshot(now=1101)
        self.assertEqual(settled["balance"], 800)
        self.assertEqual(settled["unknown_usage_request_count"], 0)

    def test_legacy_nonzero_reservation_keeps_its_prepaid_semantics(self) -> None:
        with self.storage._connect() as connection:
            connection.execute(
                "UPDATE resource_state SET balance = 700, updated_at = 1000 WHERE singleton = 1"
            )
            connection.execute(
                "INSERT INTO resource_reservations (request_id, reserved_tokens, status, created_at) VALUES (?, ?, 'reserved', ?)",
                ("legacy", 300, 1000),
            )
        self.storage.initialize_session(enabled=True, now=1000)
        self.assertEqual(self.storage.resource_snapshot(now=4600)["balance"], 700)
        self.assertTrue(self.storage.settle_usage(
            "legacy", prompt_tokens=120, completion_tokens=80,
            total_tokens=200, success=True, now=4600,
        ))
        self.assertEqual(self.storage.resource_snapshot(now=4600)["balance"], 800)

    def test_config_reload_does_not_turn_live_usage_marker_unknown(self) -> None:
        self.storage.begin_usage_request("config-reload", now=1000)
        self.storage.update_session_enabled(enabled=True, now=1100)

        snapshot = self.storage.resource_snapshot(now=1100)
        self.assertEqual(snapshot["reserved_tokens"], 0)
        self.assertEqual(snapshot["unknown_reserved_tokens"], 0)

    def test_disabling_session_preserves_usage_marker_and_reenables_session(self) -> None:
        self.storage.begin_usage_request("disable-toggle", now=1000)
        disabled = self.storage.update_session_enabled(enabled=False, now=1100)
        reenabled = self.storage.update_session_enabled(enabled=True, now=1200)

        self.assertEqual(disabled["status"], "DISABLED")
        self.assertEqual(reenabled["status"], "ACTIVE")
        snapshot = self.storage.resource_snapshot(now=1200)
        self.assertEqual(snapshot["reserved_tokens"], 0)
        self.assertEqual(snapshot["unknown_reserved_tokens"], 0)

    def test_new_session_is_stopped_when_auto_start_is_not_requested(self) -> None:
        fresh = Storage(Path(self.temp_dir.name) / "manual-start.sqlite3")
        session = fresh.initialize_session(enabled=True, now=1000)
        self.assertEqual(session["status"], "STOPPED")
        self.assertEqual(session["run_requested"], 0)
        fresh.start_session(now=1001)
        restarted = fresh.initialize_session(enabled=True, now=2000)
        self.assertEqual(restarted["status"], "DISCONNECTED")
        self.assertEqual(restarted["run_requested"], 1)

    def test_initialize_session_requires_explicit_auto_start(self) -> None:
        fresh = Storage(Path(self.temp_dir.name) / "default-stop.sqlite3")
        session = fresh.initialize_session(enabled=True, auto_start=True, now=1000)
        self.assertEqual(session["status"], "ACTIVE")
        self.assertEqual(session["run_requested"], 1)

    def test_requested_stop_during_api_wait_cannot_become_disconnect_retry(self) -> None:
        self.storage.start_session(now=1001)
        self.storage.request_session_stop(now=1002)
        finished = self.storage.finish_agent_run(
            normal=False, last_error="请求已取消", retry_initial_seconds=5, retry_multiplier=2, retry_max_seconds=600, now=1003,
        )
        self.assertEqual(finished["status"], "STOPPED")
        self.assertIsNone(finished["next_retry_at"])

    def test_requested_stop_remains_stopped_after_plugin_reload(self) -> None:
        self.storage.request_session_stop(now=1001)
        restarted = self.storage.initialize_session(enabled=True, now=2000)

        self.assertEqual(restarted["status"], "STOPPED")
        self.assertEqual(restarted["run_requested"], 0)
        self.assertIsNone(restarted["next_retry_at"])

    def test_start_graceful_stop_and_finish_keep_explicit_lifecycle(self) -> None:
        stopped = self.storage.request_session_stop(now=1001)
        self.assertEqual(stopped["status"], "STOP_REQUESTED")
        self.assertEqual(stopped["stop_requested"], 1)
        finished = self.storage.finish_agent_run(
            normal=True, retry_initial_seconds=5, retry_multiplier=2, retry_max_seconds=600, now=1002,
        )
        self.assertEqual(finished["status"], "STOPPED")
        self.assertEqual(finished["run_requested"], 0)
        started = self.storage.start_session(now=1003)
        self.assertEqual(started["status"], "ACTIVE")
        self.assertEqual(started["run_requested"], 1)
        finished_without_rest = self.storage.finish_agent_run(
            normal=True, retry_initial_seconds=5, retry_multiplier=2, retry_max_seconds=600, now=1004,
        )
        self.assertEqual(finished_without_rest["status"], "RESTING")
        self.assertGreater(finished_without_rest["rest_until"], 1004)

    def test_force_stop_persists_disabled_run_and_marks_uncertain_actions(self) -> None:
        self.storage.start_action("uncertain-step", "phone.use", {"action": "tap"}, now=1001)
        self.storage.begin_usage_request("uncertain-usage", now=1001)
        self.storage.force_stop_session(now=1002)
        stopped = self.storage.complete_force_stop(now=1003)
        self.assertEqual(stopped["status"], "STOPPED")
        self.assertEqual(stopped["run_requested"], 0)
        action = self.storage.get_action_by_step("uncertain-step")
        self.assertEqual(action["status"], "outcome_unknown")
        self.assertEqual(self.storage.resource_snapshot(now=1003)["unknown_usage_request_count"], 1)

    def test_due_rest_wakes_once_and_keeps_events_queued_until_agent_runs(self) -> None:
        self.storage.request_rest(60, "休息", now=1000)
        self.storage.finish_agent_run(
            normal=True,
            retry_initial_seconds=5,
            retry_multiplier=2,
            retry_max_seconds=600,
            now=1000,
        )
        self.storage.enqueue_event("interest_recorded", {"external_id": "i-1"}, now=1010)

        self.assertFalse(self.storage.activate_due_rest(now=1059))
        self.assertTrue(self.storage.activate_due_rest(now=1060))
        self.assertFalse(self.storage.activate_due_rest(now=1061))
        snapshot = self.storage.get_status_snapshot(now=1061)
        self.assertEqual(snapshot["session"]["status"], "ACTIVE")
        self.assertEqual(snapshot["queued_event_count"], 2)

    def test_stability_timer_only_runs_while_session_is_active(self) -> None:
        self.storage.set_session_status("DISABLED", now=1005)
        self.storage.mark_session_stable(now=1010)
        self.assertIsNone(self.storage.get_session()["stable_since"])
        self.assertFalse(self.storage.clear_reconnect_after_stable(60, now=1070))

        self.storage.set_session_status("ACTIVE", now=1080)
        self.storage.mark_session_stable(now=1080)
        self.assertFalse(self.storage.clear_reconnect_after_stable(60, now=1139))
        self.assertTrue(self.storage.clear_reconnect_after_stable(60, now=1140))

    def test_rest_and_retry_reject_invalid_limits(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.request_rest(30, "invalid cap", maximum_seconds=0, now=1000)
        with self.assertRaises(ValueError):
            self.storage.finish_agent_run(
                normal=False,
                retry_initial_seconds=30,
                retry_multiplier=2,
                retry_max_seconds=10,
                now=1000,
            )

    def test_restart_during_unconfirmed_rest_becomes_disconnected(self) -> None:
        self.storage.request_rest(300, "准备休息", now=1000)
        restored = self.storage.initialize_session(enabled=True, now=1100)
        self.assertEqual(restored["status"], "DISCONNECTED")
        self.assertIsNone(restored["rest_until"])
        self.assertEqual(restored["next_retry_at"], 1105)

    def test_restart_during_active_session_uses_persisted_backoff(self) -> None:
        self.storage.start_session(now=1000)
        self.storage.set_session_status("ACTIVE", now=1001)
        restarted = self.storage.initialize_session(
            enabled=True,
            retry_initial_seconds=5,
            retry_multiplier=2,
            retry_max_seconds=600,
            now=1010,
        )
        self.assertEqual(restarted["status"], "DISCONNECTED")
        self.assertEqual(restarted["reconnect_attempts"], 1)
        self.assertEqual(restarted["reconnect_delay_seconds"], 5)
        self.assertEqual(restarted["next_retry_at"], 1015)

    def test_legacy_auto_started_active_session_is_recovered_after_upgrade(self) -> None:
        self.storage.start_session(now=1000)
        with self.storage._connect() as connection:
            connection.execute("UPDATE will_session SET run_requested = 0, status = 'ACTIVE' WHERE singleton = 1")
        migrated = self.storage.initialize_session(enabled=True, now=1010)
        self.assertEqual(migrated["status"], "DISCONNECTED")
        self.assertEqual(migrated["run_requested"], 1)

    def test_queued_event_interrupts_rest_only_for_an_autonomous_session(self) -> None:
        self.storage.initialize_session(enabled=True, auto_start=True, now=1000)
        self.storage.request_rest(3600, "自主休息", now=1000)
        self.storage.finish_agent_run(
            normal=True,
            retry_initial_seconds=5,
            retry_multiplier=2,
            retry_max_seconds=600,
            now=1000,
        )
        self.storage.enqueue_event("operator.message", {"content": "现在看看"}, now=1001)

        self.assertTrue(self.storage.wake_rest_for_events(now=1001))
        session = self.storage.get_session()
        self.assertEqual(session["status"], "ACTIVE")
        self.assertIsNone(session["rest_until"])
        self.assertEqual(self.storage.get_pending_events()[0]["payload"]["content"], "现在看看")

    def test_explicit_stop_is_not_undone_by_autostart_after_reload(self) -> None:
        self.storage.initialize_session(enabled=True, auto_start=True, now=1000)
        self.storage.request_session_stop(now=1001)
        self.storage.finish_agent_run(
            normal=True,
            retry_initial_seconds=5,
            retry_multiplier=2,
            retry_max_seconds=600,
            now=1002,
        )

        restarted = self.storage.initialize_session(enabled=True, auto_start=True, now=2000)
        self.assertEqual(restarted["status"], "STOPPED")
        self.assertEqual(restarted["manual_stop"], 1)
        self.assertEqual(restarted["run_requested"], 0)

    def test_legacy_resource_blocked_sessions_remain_stopped_without_explicit_start(self) -> None:
        for status in ("BUDGET_BLOCKED", "ENERGY_WAIT"):
            self.storage.set_session_status(status, now=1000)
            upgraded = self.storage.initialize_session(enabled=True, now=1010)
            self.assertEqual(upgraded["status"], "STOPPED")

    def test_default_resource_values_match_configured_initial_pool(self) -> None:
        from maiwill.config import ResourceConfig

        config = ResourceConfig()
        self.assertEqual(config.capacity_tokens, 10_000_000)
        self.assertEqual(config.recovery_tokens_per_hour, 1_000_000)
        self.assertTrue(config.start_at_capacity)

        fresh_storage = Storage(Path(self.temp_dir.name) / "fresh.sqlite3")
        fresh_storage.initialize_session(enabled=True, auto_start=True, now=2000)
        state = fresh_storage.sync_resource_config(
            config.capacity_tokens,
            config.recovery_tokens_per_hour,
            start_at_capacity=config.start_at_capacity,
            now=2000,
        )
        self.assertEqual(state["balance"], 10_000_000)

        fresh_storage.begin_usage_request("configured-pool", now=2000)
        fresh_storage.settle_usage(
            "configured-pool",
            prompt_tokens=1_200_000,
            completion_tokens=800_000,
            total_tokens=2_000_000,
            success=True,
            now=2001,
        )
        self.assertEqual(fresh_storage.resource_snapshot(now=3801)["balance"], 8_500_000)

    def test_general_config_rejects_reconnect_max_below_initial(self) -> None:
        from pydantic import ValidationError

        from maiwill.config import GeneralConfig

        with self.assertRaises(ValidationError):
            GeneralConfig(reconnect_initial_seconds=30, reconnect_max_seconds=10)


if __name__ == "__main__":
    unittest.main()
