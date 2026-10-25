from __future__ import annotations

import unittest

from maiwill.persona import PERSONA_FIELD_LIMITS, format_persona_reference


class PersonaReferenceTest(unittest.TestCase):
    def test_formats_the_three_persona_elements_with_clear_scope(self) -> None:
        result = format_persona_reference(
            name="麦麦",
            personality="好奇、坦率。",
            behavior_style="主动探索，但不强行找话题。",
            reply_style="简短自然。",
        )

        self.assertIn("名字：麦麦", result)
        self.assertIn("人格设定：好奇、坦率。", result)
        self.assertIn("行为风格：主动探索，但不强行找话题。", result)
        self.assertIn("表达风格：简短自然。", result)
        self.assertIn("低体力优先休息规则", result)
        self.assertIn("所有自然语言内容，包括 reason 与 reply", result)
        self.assertIn("工具参数有效", result)

    def test_empty_values_do_not_add_empty_persona_section(self) -> None:
        self.assertEqual(format_persona_reference(), "")

    def test_each_field_is_whitespace_normalized_and_bounded(self) -> None:
        result = format_persona_reference(
            name="麦麦", personality="人格 " * (PERSONA_FIELD_LIMITS["personality"] + 30),
            behavior_style="行动\n准则", reply_style="表达\t习惯",
        )

        self.assertLessEqual(len(result.split("人格设定：", 1)[1].splitlines()[0]), PERSONA_FIELD_LIMITS["personality"])
        self.assertIn("行动 准则", result)
        self.assertIn("表达 习惯", result)
        self.assertTrue(result.endswith("…") or "。" in result)


if __name__ == "__main__":
    unittest.main()
