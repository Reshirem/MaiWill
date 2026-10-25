from __future__ import annotations

import json
import unittest

from maiwill.decision import DecisionFormatError, decision_schema, parse_decision_response


class DecisionResponseTest(unittest.TestCase):
    def assert_rejected(self, response, code: str) -> DecisionFormatError:
        with self.assertRaises(DecisionFormatError) as caught:
            parse_decision_response(response, available_capabilities={"phone.use"})
        self.assertEqual(caught.exception.diagnostics["error_code"], code)
        return caught.exception

    def test_valid_decisions_preserve_arguments_and_escaped_text(self) -> None:
        decisions = [
            {
                "decision": "use_capability", "capability": "phone.use",
                "arguments": {"action": "input", "text": '含有 {括号}、"引号"、\\路径\n和换行'},
                "reason": "继续操作", "reply": "收到。",
            },
            {"decision": "rest", "duration_seconds": 30},
            {"decision": "finish", "reason": "响应停止请求"},
        ]
        for decision in decisions:
            with self.subTest(decision=decision["decision"]):
                self.assertEqual(
                    parse_decision_response(json.dumps(decision, ensure_ascii=False), available_capabilities={"phone.use"}),
                    decision,
                )

    def test_complete_json_fences_and_bom_do_not_need_another_model_request(self) -> None:
        text = '{"decision":"rest","duration_seconds":30}'
        for response in (
            " \n" + text + "\n ", "\ufeff" + text,
            "```json\n" + text + "\n```", "```\n" + text + "\n```",
            " \ufeff```JSON\r\n" + text + "\r\n``` ",
        ):
            with self.subTest(response=response[:12]):
                self.assertEqual(parse_decision_response(response), json.loads(text))

    def test_does_not_extract_an_action_from_prose_arrays_or_multiple_objects(self) -> None:
        action = '{"decision":"use_capability","capability":"phone.use","arguments":{"action":"tap","x":1,"y":2}}'
        for response in (
            "解释：" + action, action + "完成", action + "\n" + action,
            "```json\n" + action + "\n```\n" + action,
            "```json\n" + action, "```python\n" + action + "\n```",
            "<think>" + action + "</think>",
        ):
            with self.subTest(response=response[:30]):
                self.assert_rejected(response, "invalid_json")
        self.assert_rejected("[" + action + "]", "not_object")

    def test_syntax_failure_reports_location_without_copying_response(self) -> None:
        response = '{\n"decision":"rest",\n"duration_seconds":30,\n"reply":"PRIVATE-RESPONSE",\n}'
        error = self.assert_rejected(response, "invalid_json")
        self.assertIn("第 5 行，第 1 列", str(error))
        self.assertEqual(error.diagnostics["line"], 5)
        self.assertEqual(error.diagnostics["column"], 1)
        self.assertEqual(error.diagnostics["response_chars"], len(response))
        self.assertEqual(len(error.diagnostics["response_sha256"]), 64)
        self.assertNotIn("PRIVATE-RESPONSE", json.dumps(error.diagnostics) + str(error))

    def test_truncated_or_invalid_strings_are_not_repaired(self) -> None:
        for response in (
            '{"decision":"rest","duration_seconds":30',
            '{"decision":"rest","duration_seconds":30,"reply":"未结束',
            '{"decision":"rest","duration_seconds":30,"reply":"未转义\n换行"}',
            "{'decision':'rest','duration_seconds':30}",
        ):
            with self.subTest(response=response):
                self.assert_rejected(response, "invalid_json")

    def test_missing_empty_and_non_object_responses_are_distinct(self) -> None:
        for response in (None, {}, 123):
            self.assert_rejected(response, "missing_text")
        for response in ("", " \n", "\ufeff ", "```json\n\n```"):
            self.assert_rejected(response, "empty_response")
        for response in ("null", "[]", '"rest"', "123", "true"):
            self.assert_rejected(response, "not_object")

    def test_ambiguous_keys_and_non_finite_numbers_are_rejected(self) -> None:
        for response in (
            '{"decision":"rest","decision":"finish","duration_seconds":30}',
            '{"decision":"use_capability","capability":"phone.use","arguments":{"x":1,"x":2}}',
        ):
            self.assert_rejected(response, "duplicate_key")
        for number in ("NaN", "Infinity", "-Infinity", "1e999"):
            self.assert_rejected(
                '{"decision":"use_capability","capability":"phone.use","arguments":{"x":' + number + '}}',
                "invalid_number",
            )

    def test_decision_fields_are_checked_before_an_action_can_execute(self) -> None:
        for decision, code in (
            ({}, "invalid_kind"),
            ({"decision": "remember"}, "invalid_kind"),
            ({"decision": "rest", "duration_seconds": True}, "invalid_duration"),
            ({"decision": "rest", "duration_seconds": "30"}, "invalid_duration"),
            ({"decision": "rest", "duration_seconds": 0}, "invalid_duration"),
            ({"decision": "use_capability", "arguments": {}}, "invalid_capability"),
            ({"decision": "use_capability", "capability": "phone.tap", "arguments": {}}, "unavailable_capability"),
            ({"decision": "use_capability", "capability": " phone.use", "arguments": {}}, "unavailable_capability"),
            ({"decision": "use_capability", "capability": "phone.use", "arguments": "{}"}, "invalid_arguments"),
            ({"decision": "rest", "duration_seconds": 30, "reason": "字" * 1001}, "invalid_reason"),
            ({"decision": "rest", "duration_seconds": 30, "reply": "字" * 4001}, "invalid_reply"),
            ({"decision": "finish", "_usage_request_id": "model-supplied"}, "unexpected_fields"),
            ({"decision": "finish", "capability": "phone.use"}, "unexpected_fields"),
        ):
            with self.subTest(code=code, decision=decision["decision"] if "decision" in decision else "missing"):
                self.assert_rejected(json.dumps(decision), code)

    def test_prompt_schema_uses_only_current_capability_names(self) -> None:
        branches = decision_schema({"phone.use", "diary.list"})["oneOf"]
        action = branches[0]
        self.assertEqual(action["properties"]["capability"]["enum"], ["diary.list", "phone.use"])
        self.assertEqual(action["required"], ["decision", "capability", "arguments"])
        self.assertEqual(branches[1]["required"], ["decision", "duration_seconds"])
        self.assertTrue(all(branch["additionalProperties"] is False for branch in branches))


if __name__ == "__main__":
    unittest.main()
