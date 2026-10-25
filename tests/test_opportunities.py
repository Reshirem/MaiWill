from __future__ import annotations

import unittest

from maiwill.opportunities import build_exploration_options


class ExplorationOptionsTest(unittest.TestCase):
    def test_lists_actionable_existing_goals_interests_candidates_and_phone_discovery(self) -> None:
        options = build_exploration_options(
            available_capabilities=[
                {"name": "phone.use"}, {"name": "reflection.list"},
                {"name": "memory.search"}, {"name": "diary.write"},
            ],
            learning_candidates=[{
                "external_id": "candidate-1", "title": "未整理主题", "status": "pending",
            }],
            recent_interests=[{
                "external_id": "interest-1", "topic": "海洋", "reason": "想继续了解",
            }],
            exploration_goals=[{
                "goal_id": "goal-1", "title": "继续研究潮汐", "status": "active",
                "next_step": "检查已有记录", "is_stalled": False,
            }],
            recent_actions=[{
                "action_id": "action-1", "capability": "diary.write", "result_summary": "日记已保存",
            }],
            context_message_count=3,
        )

        kinds = [option["kind"] for option in options]
        self.assertEqual(kinds[0], "continue_goal")
        self.assertIn("review_learning_candidates", kinds)
        self.assertIn("follow_recorded_interest", kinds)
        self.assertIn("discover_configured_phone", kinds)
        self.assertIn("build_on_recent_action", kinds)
        self.assertIn("reflect_on_session", kinds)

    def test_empty_sources_allow_device_interaction_for_self_directed_discovery(self) -> None:
        options = build_exploration_options(
            available_capabilities=[{"name": "phone.use"}], learning_candidates=[],
            recent_interests=[], exploration_goals=[], recent_actions=[], context_message_count=0,
        )

        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]["kind"], "self_directed_discovery")
        self.assertIn("不代表无事可做", options[0]["next_step"])
        self.assertIn("设备内 shell/ADB", options[0]["next_step"])
        self.assertIn("配置设备", options[0]["next_step"])
        self.assertIn("不购买", options[0]["next_step"])

    def test_self_directed_discovery_is_available_without_goals_after_phone_observation(self) -> None:
        options = build_exploration_options(
            available_capabilities=[{"name": "phone.use"}, {"name": "diary.write"}],
            learning_candidates=[], recent_interests=[], exploration_goals=[],
            recent_actions=[{"capability": "phone.use", "action_kind": "screen"}],
            context_message_count=0,
        )

        self.assertNotIn("discover_configured_phone", [option["kind"] for option in options])
        discovery = next(option for option in options if option["kind"] == "self_directed_discovery")
        self.assertEqual(discovery["available_capabilities"], ["phone.use"])
        self.assertIn("单步发现", discovery["next_step"])
        self.assertIn("设备内 shell/ADB", discovery["next_step"])

    def test_disables_phone_discovery_after_recent_read_only_observation(self) -> None:
        options = build_exploration_options(
            available_capabilities=[{"name": "phone.use"}], learning_candidates=[],
            recent_interests=[], exploration_goals=[],
            recent_actions=[{"capability": "phone.use", "action_kind": "screen"}],
            context_message_count=0,
        )

        self.assertNotIn("discover_configured_phone", [option["kind"] for option in options])
        discovery = next(option for option in options if option["kind"] == "self_directed_discovery")
        self.assertIn("真实可见页面上的操作", discovery["next_step"])

    def test_no_sources_or_capabilities_does_not_invent_tasks(self) -> None:
        self.assertEqual(build_exploration_options(
            available_capabilities=[], learning_candidates=[], recent_interests=[],
            exploration_goals=[], recent_actions=[], context_message_count=0,
        ), [])

    def test_local_reflection_capability_alone_allows_self_directed_discovery(self) -> None:
        options = build_exploration_options(
            available_capabilities=[{"name": "reflection.create"}], learning_candidates=[],
            recent_interests=[], exploration_goals=[], recent_actions=[], context_message_count=0,
        )

        self.assertEqual([option["kind"] for option in options], ["self_directed_discovery"])


if __name__ == "__main__":
    unittest.main()
