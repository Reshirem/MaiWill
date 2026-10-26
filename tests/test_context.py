from __future__ import annotations

import unittest

from maiwill.context import compress_context_if_needed, estimate_message_tokens


class ContextManagementTest(unittest.TestCase):
    def test_compression_waits_for_threshold_and_uses_prompt_usage(self) -> None:
        context = {"messages": [{"role": "observation", "text": "x" * 1000}]}
        original = list(context["messages"])

        self.assertEqual(
            compress_context_if_needed(
                context, prompt_tokens=79, limit_tokens=100, threshold=0.8
            ),
            0,
        )
        self.assertEqual(context["messages"], original)
        self.assertEqual(
            compress_context_if_needed(
                context, prompt_tokens=None, limit_tokens=100, threshold=0.8
            ),
            0,
        )

    def test_compression_trims_old_complete_units_and_preserves_recent_tool_roundtrip(self) -> None:
        old = {"role": "observation", "text": "早" * 600}
        assistant_call = {
            "role": "assistant",
            "decision": "use_capability",
            "capability": "diary.write",
            "arguments": {"title": "近况"},
        }
        result = {
            "role": "capability_result",
            "capability": "diary.write",
            "result": {"success": True},
        }
        recent = {"role": "assistant", "decision": "rest", "duration_seconds": 30}
        context = {"messages": [old, assistant_call, result, recent]}

        discarded = compress_context_if_needed(
            context, prompt_tokens=800, limit_tokens=1000, threshold=0.8
        )

        self.assertEqual(discarded, 1)
        self.assertEqual(context["messages"][0]["role"], "history_compacted")
        self.assertNotIn("summary", context["messages"][0])
        self.assertEqual(context["messages"][1:], [assistant_call, result, recent])

    def test_incomplete_capability_records_are_pinned_and_never_split(self) -> None:
        incomplete = {
            "role": "assistant",
            "decision": "use_capability",
            "capability": "phone.use",
        }
        context = {"messages": [incomplete]}

        discarded = compress_context_if_needed(
            context, prompt_tokens=80, limit_tokens=100, threshold=0.8
        )

        self.assertEqual(discarded, 0)
        self.assertEqual(context["messages"], [incomplete])

    def test_pending_screenshot_roundtrip_is_not_compressed_before_visual_input(self) -> None:
        call = {
            "role": "assistant",
            "decision": "use_capability",
            "capability": "phone.use",
            "arguments": {"action": "screenshot"},
        }
        result = {
            "role": "capability_result",
            "capability": "phone.use",
            "result": {"success": True, "status": "screenshot", "path": "phone/screenshots/1234567890abcdef1234567890abcdef.png"},
        }
        context = {"messages": [{"role": "observation", "text": "old" * 1000}, call, result]}

        compress_context_if_needed(context, prompt_tokens=800, limit_tokens=1000, threshold=0.8)

        self.assertEqual(context["messages"][0]["role"], "history_compacted")
        self.assertEqual(context["messages"][1:], [call, result])

    def test_estimator_counts_cjk_and_serialized_message_shape(self) -> None:
        message = {"role": "observation", "text": "你好abc"}
        self.assertGreaterEqual(estimate_message_tokens(message), 4)

    def test_direct_adb_pending_screenshot_is_kept_during_compaction(self) -> None:
        call = {"role": "assistant", "decision": "use_capability", "capability": "phone.adb", "arguments": {"argv": ["exec-out", "screencap", "-p"]}}
        result = {"role": "capability_result", "capability": "phone.adb", "result": {"success": True, "status": "screenshot", "path": "phone/screenshots/1234567890abcdef1234567890abcdef.png"}}
        context = {"messages": [{"role": "observation", "text": "old" * 1000}, call, result]}
        compress_context_if_needed(context, prompt_tokens=800, limit_tokens=1000, threshold=0.8)
        self.assertEqual(context["messages"][1:], [call, result])


if __name__ == "__main__":
    unittest.main()
