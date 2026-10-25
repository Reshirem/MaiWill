from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel, Field

from maiwill.agent import AgentLoop
from maiwill.context import compress_context_if_needed
from maiwill.records import MAX_DOCUMENT_BYTES, get_diary, list_diaries, read_diary
from maiwill.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "_maibot_plugin_records_test", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)],
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
# Other existing suites install a minimal config-only SDK stub at collection time.
# Keep this plugin fixture self-contained and restore those modules after loading.
SDK = ModuleType("maibot_sdk")
SDK.__path__ = []
SDK.API = lambda *args, **kwargs: lambda function: function
SDK.Tool = lambda *args, **kwargs: lambda function: function
SDK.MaiBotPlugin = type("MaiBotPlugin", (), {})
SDK.PluginConfigBase = BaseModel
SDK.Field = Field
SDK_TYPES = ModuleType("maibot_sdk.types")
SDK_TYPES.ToolParameterInfo = lambda **kwargs: SimpleNamespace(**kwargs)
SDK_TYPES.ToolParamType = SimpleNamespace(STRING="string", ARRAY="array")
with patch.dict(sys.modules, {"maibot_sdk": SDK, "maibot_sdk.types": SDK_TYPES}):
    SPEC.loader.exec_module(MODULE)


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.storage = Storage(self.root / "maiwill.sqlite3")
        self.storage.initialize_session(enabled=True)
        self.storage.sync_resource_config(1_000_000, 100_000, start_at_capacity=True)
        self.plugin = MODULE.MaiWillPlugin()
        self.plugin._storage = self.storage
        self.plugin._data_dir = self.root

    def diary(self, title="潮汐观察", content="今天看到了潮水。"):
        return self.plugin._write_diary_entry(self.root, title, content)

    def candidate(self, index=0, *, now=None, content=None):
        return self.storage.create_learning_candidate(
            "mai_will.knowledge", f"潮汐研究 {index}", content or f"正文 {index}：🌊 记录真实观察。",
            ["海洋", "100%"], origin_session_id="old-session", source_ref="diary:fixture", now=now,
        )

    def test_existing_windows_diaries_and_complete_unicode_reads(self):
        original = "第一行\n潮汐🌊，" * 1500
        result = self.diary(content=original)
        # The old Windows writer emits CRLF, while the public body remains normalized.
        path = self.root / result["path"]
        normalized = path.read_text(encoding="utf-8")
        path.write_bytes(normalized.replace("\n", "\r\n").encode("utf-8"))
        listed = list_diaries(self.root, query="潮汐")
        self.assertEqual(listed["entries"][0]["entry_id"], result["entry_id"])
        offset, revision, pieces = 0, "", []
        while True:
            part = read_diary(self.root, result["entry_id"], offset=offset, limit=999, revision=revision)
            pieces.append(part["content"])
            self.assertLessEqual(len(part["content"]), 999)
            if part["next_offset"] is None:
                break
            offset, revision = part["next_offset"], part["revision"]
        self.assertEqual("".join(pieces), original)
        self.assertTrue(part["success"])

    def test_diary_pagination_filters_and_bad_file_reporting(self):
        ids = {self.diary(f"观察 {i:02}", f"海潮 {i}")["entry_id"] for i in range(27)}
        found, cursor = [], ""
        while True:
            page = list_diaries(self.root, query="海潮", cursor=cursor, limit=7)
            found.extend(item["entry_id"] for item in page["entries"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        self.assertEqual(len(found), 27)
        self.assertEqual(set(found), ids)
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(list_diaries(self.root, date_from=today, date_to=today)["count"], 10)
        self.assertEqual(list_diaries(self.root, query="不存在")["count"], 0)
        (self.root / "diary" / "2026-10-25-bad-aaaaaaaaaaaa.md").write_text("坏文件", encoding="utf-8")
        page = list_diaries(self.root, query="不存在")
        self.assertEqual(page["skipped_count"], 1)
        with self.assertRaises(ValueError):
            list_diaries(self.root, cursor=cursor, query="不同筛选")

    def test_invalid_ids_bounds_dates_and_cursors_are_rejected(self):
        result = self.diary()
        for record_id in ("../outside.md", "C:/secret", "", None, "A" * 12):
            with self.subTest(record_id=record_id), self.assertRaises(ValueError):
                get_diary(self.root, record_id)
        for args in ({"limit": True}, {"limit": 21}, {"date_from": "2026-02-30"},
                     {"date_from": "2026-10-25", "date_to": "2026-10-24"}, {"cursor": "not-a-cursor"},
                     {"query": "a" * 201}, {"cursor": "W10="}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                list_diaries(self.root, **args)
        for args in ({"offset": -1}, {"offset": True}, {"offset": 1}, {"limit": 4001}, {"revision": "stale"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                read_diary(self.root, result["entry_id"], **args)
        with self.assertRaises(ValueError):
            self.storage.query_learning_candidates(date_to="9999-12-31")

    def test_linked_oversized_and_nonregular_diary_files_are_rejected(self):
        result = self.diary()
        path = self.root / result["path"]
        source = path.read_bytes()
        path.write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "128 KiB"):
            get_diary(self.root, result["entry_id"])
        path.write_bytes(source)
        os.link(path, self.root / "linked.md")
        with self.assertRaisesRegex(ValueError, "链接"):
            get_diary(self.root, result["entry_id"])
        with patch("maiwill.records.Path.lstat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)):
            with self.assertRaisesRegex(ValueError, "junction"):
                list_diaries(self.root)

    def test_revision_change_prevents_mixing_pages(self):
        result = self.diary(content="旧正文" * 3000)
        first = read_diary(self.root, result["entry_id"], limit=100)
        path = self.root / result["path"]
        path.write_text(path.read_text(encoding="utf-8").replace("旧正文", "新正文"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "正文已变化"):
            read_diary(self.root, result["entry_id"], offset=100, revision=first["revision"])

    def test_candidate_pages_are_complete_with_ties_new_insertions_and_resolved_items(self):
        ids = {self.candidate(i, now=1000 + i // 3)["external_id"] for i in range(27)}
        for external_id in ids:
            self.storage.update_learning_candidate_status(external_id, "resolved")
        first = self.storage.query_learning_candidates(status="resolved", limit=5)
        self.candidate(100, now=99999)
        found = [item["external_id"] for item in first["candidates"]]
        cursor = first["next_cursor"]
        while cursor:
            page = self.storage.query_learning_candidates(status="resolved", cursor=cursor, limit=5)
            found.extend(item["external_id"] for item in page["candidates"])
            cursor = page["next_cursor"]
        self.assertEqual(len(found), 27)
        self.assertEqual(set(found), ids)
        self.assertNotIn("content", first["candidates"][0])
        self.assertEqual(self.storage.query_learning_candidates(query="100%", limit=20)["count"], 20)
        self.assertEqual(self.storage.query_learning_candidates(query="' OR 1=1 --")["count"], 0)
        with self.assertRaises(ValueError):
            self.storage.query_learning_candidates(cursor=first["next_cursor"], status="pending")

    def test_candidate_dates_deleted_ids_and_complete_body(self):
        now = datetime(2026, 10, 25, 12).timestamp()
        item = self.candidate(now=now, content="🌊真实资料\n" * 1500)
        page = self.storage.query_learning_candidates(date_from="2026-10-25", date_to="2026-10-25")
        self.assertEqual(page["count"], 1)
        self.assertEqual(self.storage.query_learning_candidates(date_to="2026-10-24")["count"], 0)
        first = self.storage.read_learning_candidate(item["external_id"], limit=4000)
        second = self.storage.read_learning_candidate(item["external_id"], offset=4000, revision=first["revision"])
        third = self.storage.read_learning_candidate(item["external_id"], offset=8000, revision=first["revision"])
        self.assertEqual(first["content"] + second["content"] + third["content"], item["content"])
        self.assertEqual(first["memory_sync_status"], "not_synced")
        self.storage.delete_learning_candidate(item["external_id"])
        with self.assertRaisesRegex(ValueError, "找不到"):
            self.storage.read_learning_candidate(item["external_id"])

    def test_export_is_complete_idempotent_and_rejects_stale_revision(self):
        diary = self.diary(content="保存完整正文🌊" * 1000)
        reflection = self.candidate(content="学习正文" * 2000)
        for kind, record_id, expected in (("diary", diary["entry_id"], "保存完整正文🌊" * 1000),
                                           ("reflection", reflection["external_id"], reflection["content"])):
            result = asyncio.run(self.plugin.will_export_record(kind, record_id))
            self.assertIn(expected, (self.root / result["path"]).read_text(encoding="utf-8"))
            self.assertEqual(result, asyncio.run(self.plugin.will_export_record(kind, record_id)))
            self.assertTrue(Path(result["saved_to"]).is_relative_to(self.root))
            with self.assertRaisesRegex(ValueError, "正文已变化"):
                asyncio.run(self.plugin.will_export_record(kind, record_id, revision="old"))
        self.assertEqual(len(list((self.root / "exports").glob("*.md"))), 2)
        self.assertFalse(list((self.root / "exports").glob("*.tmp")))

    def test_export_rejects_conflicting_target(self):
        diary = self.diary()
        result = asyncio.run(self.plugin.will_export_record("diary", diary["entry_id"]))
        path = self.root / result["path"]
        path.write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "内容不匹配"):
            asyncio.run(self.plugin.will_export_record("diary", diary["entry_id"]))

    def test_model_tools_reject_unknown_fields_and_return_bounded_results(self):
        diary = self.diary()
        candidate = self.candidate()
        self.assertTrue(asyncio.run(self.plugin._list_diaries({}))["success"])
        self.assertTrue(asyncio.run(self.plugin._read_diary({"entry_id": diary["entry_id"]}))["success"])
        self.assertTrue(asyncio.run(self.plugin._list_reflections({"status": None}))["success"])
        self.assertTrue(asyncio.run(self.plugin._read_reflection({"external_id": candidate["external_id"]}))["success"])
        self.assertFalse(asyncio.run(self.plugin._read_diary({"entry_id": diary["entry_id"], "path": "../outside"}))["success"])
        self.assertFalse(asyncio.run(self.plugin._list_diaries({"kind": "reflection"}))["success"])
        page = self.storage.query_learning_candidates(limit=20)
        self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 512 * 1024)

    def test_recall_survives_compaction_and_new_session_in_one_agent_loop(self):
        diary = self.diary(content="跨会话保留的真实观察")
        candidate = self.candidate(content="之后仍需研究的材料")
        self.storage.update_learning_candidate_status(candidate["external_id"], "resolved")
        context = {"messages": [{"role": "observation", "text": "旧资料" * 400}]}
        self.assertGreater(compress_context_if_needed(context, prompt_tokens=2000, limit_tokens=1000, threshold=.8), 0)
        self.storage.save_session_context(context)
        self.storage.create_new_session()
        # Reopen storage, as a plugin reload would, without changing stored artifacts.
        self.storage = Storage(self.root / "maiwill.sqlite3")
        self.plugin._storage = self.storage
        self.storage.start_session()
        session_id = self.storage.get_session()["session_id"]
        decisions = [
            {"decision": "use_capability", "capability": "diary.read", "arguments": {"entry_id": diary["entry_id"]}},
            {"decision": "use_capability", "capability": "reflection.read", "arguments": {"external_id": candidate["external_id"]}},
            {"decision": "rest", "duration_seconds": 60},
        ]
        observed = []

        async def provider(current, context, events):
            self.assertEqual(current, session_id)
            observed[:] = list(context["messages"])
            return decisions.pop(0)

        loop = AgentLoop(self.storage, provider, {"diary.read": self.plugin._read_diary,
                                                "reflection.read": self.plugin._read_reflection})
        self.assertTrue(asyncio.run(loop.run()))
        bodies = [m["result"]["content"] for m in observed if m.get("role") == "capability_result"]
        self.assertEqual(bodies, ["跨会话保留的真实观察", "之后仍需研究的材料"])
        self.assertEqual(self.storage.get_recent_successful_actions(), [])


if __name__ == "__main__":
    unittest.main()
