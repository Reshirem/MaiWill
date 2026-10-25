from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from maiwill.memory_bridge import WebUIMemoryBridge, read_webui_token, validate_loopback_url

EXTERNAL_ID = "mai-will:learning:sha256:" + "a" * 64
MEMORY_ID = "b" * 64
FILENAME = "maiwill-" + "a" * 24 + ".json"
SOURCE = "web_import:" + FILENAME


class MemoryBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "webui.json").write_text(
            json.dumps({"access_token": "private-test-token"}), encoding="utf-8"
        )
        self.bridge = WebUIMemoryBridge(self.root, "http://127.0.0.1:8001")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_loopback_url_validation(self) -> None:
        self.assertEqual(validate_loopback_url(" HTTP://LOCALHOST:8001/ "), "http://127.0.0.1:8001")
        self.assertEqual(validate_loopback_url("http://[::1]:8001"), "http://[::1]:8001")
        self.assertEqual(validate_loopback_url("http://[::1]"), "http://[::1]")
        for value in ("https://example.com", "http://127.0.0.1:8001/path",
                      "http://127.0.0.1:8001/?token=x", "http://user@localhost:8001",
                      "http://localhost:", "ftp://localhost:8001"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_loopback_url(value)

    def test_token_reader_does_not_disclose_secret_on_error(self) -> None:
        self.assertEqual(read_webui_token(self.root), "private-test-token")
        (self.root / "data" / "webui.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "没有有效 Token") as raised:
            read_webui_token(self.root)
        self.assertNotIn("private-test-token", str(raised.exception))
        (self.root / "data" / "webui.json").write_text(
            json.dumps({"access_token": "bad; token"}), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "没有有效 Token"):
            read_webui_token(self.root)

    def test_request_uses_cookie_and_limits_endpoint_paths(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"success":true}'
        opener = MagicMock()
        opener.open.return_value = response
        self.bridge._opener = opener
        result = self.bridge._request("GET", "/api/webui/memory/records/search", query={"query": "test"})
        request = opener.open.call_args.args[0]
        self.assertEqual(result, {"success": True})
        self.assertEqual(request.get_header("Cookie"), "maibot_session=private-test-token")
        self.assertEqual(request.full_url, "http://127.0.0.1:8001/api/webui/memory/records/search?query=test")
        with self.assertRaises(ValueError):
            self.bridge._request("GET", "/api/webui/../plugin/config")

    def test_search_and_read_require_matching_source_and_stable_id(self) -> None:
        summary = f"[MaiWill external_id={EXTERNAL_ID}] 标题及正文"
        self.bridge._request = MagicMock(return_value={"success": True, "items": [
            {"id": MEMORY_ID, "source": SOURCE, "summary": summary, "metadata": {}},
            {"id": "c" * 64, "source": "web_import:maiwill-other.json", "summary": summary},
            {"id": "d" * 64, "source": SOURCE, "summary": "unmarked content"},
        ]})
        found = self.bridge.search_mai_will_memories("标题", 5)
        self.assertEqual([item["memory_id"] for item in found["memories"]], [MEMORY_ID])
        self.assertEqual(found["memories"][0]["external_id"], EXTERNAL_ID)
        self.bridge._request.return_value = {"success": True, "paragraph": {
            "hash": MEMORY_ID, "source": SOURCE, "content": summary + "\n正文",
        }}
        self.assertEqual(self.bridge.read_mai_will_memory(MEMORY_ID)["external_id"], EXTERNAL_ID)
        self.bridge._request.return_value["paragraph"]["content"] = "不带稳定标记"
        with self.assertRaisesRegex(ValueError, "不是 MaiWill"):
            self.bridge.read_mai_will_memory(MEMORY_ID)

    def test_read_bounds_matching_paragraph_and_reports_truncation(self) -> None:
        long_content = f"[MaiWill external_id={EXTERNAL_ID}] 标题\n" + ("完整历史 " * 5000)
        self.bridge._request = MagicMock(return_value={"success": True, "paragraph": {
            "hash": MEMORY_ID, "source": SOURCE, "content": long_content,
        }})
        result = self.bridge.read_mai_will_memory(MEMORY_ID)
        self.assertEqual(len(result["content"]), 20_000)
        self.assertTrue(long_content.startswith(result["content"]))
        self.assertTrue(result["truncated"])

    def test_search_uses_documented_maximum_result_window(self) -> None:
        summary = f"[MaiWill external_id={EXTERNAL_ID}] 标题"
        full_page = [{"id": str(index).zfill(64), "source": "other", "summary": "other"}
                     for index in range(200)]
        self.bridge._request = MagicMock(return_value={"success": True, "items": full_page})
        result = self.bridge.search_mai_will_memories("主题", 5)
        self.assertEqual(result["count"], 0)
        self.assertTrue(result["search_window_full"])
        self.assertIn("结果可能不完整", result["summary"])
        self.assertEqual(self.bridge._request.call_count, 1)
        self.assertEqual(self.bridge._request.call_args.kwargs["query"]["limit"], 200)

    def test_import_uses_leading_marker_and_confirms_completed_task(self) -> None:
        calls = []

        def request(method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            if endpoint.endswith("records/search"):
                if len([call for call in calls if call[1].endswith("records/search")]) == 1:
                    return {"success": True, "items": []}
                return {"success": True, "items": [{
                    "id": MEMORY_ID, "source": SOURCE,
                    "summary": f"[MaiWill external_id={EXTERNAL_ID}] 主题 知识内容",
                }]}
            if endpoint.endswith("paragraph-detail"):
                return {"success": True, "paragraph": {
                    "hash": MEMORY_ID, "source": SOURCE,
                    "content": f"[MaiWill external_id={EXTERNAL_ID}]\n主题\n\n知识内容\n\n标签：学习",
                }}
            if endpoint.endswith("import/paste"):
                return {"success": True, "task": {"task_id": "1234567890abcdef1234567890abcdef", "status": "queued"}}
            if endpoint.endswith("tasks/1234567890abcdef1234567890abcdef"):
                return {"success": True, "task": {"status": "completed", "files": [
                    {"status": "completed", "failed_chunks": 0,
                     "chunks": [{"status": "completed"}]},
                ]}}
            raise AssertionError(endpoint)

        self.bridge._request = request
        with patch("maiwill.memory_bridge.time.sleep"):
            result = self.bridge.create_memory_import(
                external_id=EXTERNAL_ID, title="主题", content="知识内容",
                source_type="mai_will.knowledge", tags=["学习"],
            )
        self.assertEqual(result["memory_id"], MEMORY_ID)
        post = next(call for call in calls if call[1].endswith("import/paste"))
        payload = post[2]["payload"]
        imported = json.loads(payload["content"])["paragraphs"][0]["content"]
        self.assertTrue(imported.startswith(f"[MaiWill external_id={EXTERNAL_ID}]"))
        self.assertFalse(payload["llm_enabled"])
        self.assertEqual(payload["scope_type"], "global")

    def test_retry_recognizes_existing_import(self) -> None:
        def request(method, endpoint, **kwargs):
            if endpoint.endswith("records/search"):
                return {"success": True, "items": [{
                    "id": MEMORY_ID, "source": SOURCE,
                    "summary": f"[MaiWill external_id={EXTERNAL_ID}] 标题",
                }]}
            if endpoint.endswith("paragraph-detail"):
                return {"success": True, "paragraph": {
                    "hash": MEMORY_ID, "source": SOURCE,
                    "content": f"[MaiWill external_id={EXTERNAL_ID}]\n主题\n\n知识内容",
                }}
            raise AssertionError(endpoint)
        self.bridge._request = request
        result = self.bridge.create_memory_import(
            external_id=EXTERNAL_ID, title="主题", content="知识内容",
            source_type="mai_will.knowledge", tags=[],
        )
        self.assertEqual(result["status"], "already_imported")
        self.assertEqual(result["memory_id"], MEMORY_ID)

    def test_existing_external_id_with_different_body_is_rejected(self) -> None:
        def request(method, endpoint, **kwargs):
            if endpoint.endswith("records/search"):
                return {"success": True, "items": [{
                    "id": MEMORY_ID, "source": SOURCE,
                    "summary": f"[MaiWill external_id={EXTERNAL_ID}] 标题",
                }]}
            if endpoint.endswith("paragraph-detail"):
                return {"success": True, "paragraph": {
                    "hash": MEMORY_ID, "source": SOURCE,
                    "content": f"[MaiWill external_id={EXTERNAL_ID}]\n旧标题\n\n旧正文",
                }}
            raise AssertionError(endpoint)
        self.bridge._request = request
        with self.assertRaisesRegex(RuntimeError, "正文与本地候选不一致"):
            self.bridge.create_memory_import(
                external_id=EXTERNAL_ID, title="主题", content="知识内容",
                source_type="mai_will.knowledge", tags=[],
            )

    def test_retry_refuses_ambiguous_full_search_result(self) -> None:
        self.bridge._request = MagicMock(return_value={
            "success": True, "count": 200,
            "items": [{"id": str(i).zfill(64), "source": "other", "summary": "other"} for i in range(200)],
        })
        with self.assertRaisesRegex(RuntimeError, "超过 200 条"):
            self.bridge.create_memory_import(
                external_id=EXTERNAL_ID, title="主题", content="知识内容",
                source_type="mai_will.knowledge", tags=[],
            )

    def test_resume_existing_pending_import_task(self) -> None:
        calls = []

        def request(method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            if endpoint.endswith("records/search"):
                return {"success": True, "items": []} if len(calls) == 1 else {
                    "success": True, "items": [{"id": MEMORY_ID, "source": SOURCE,
                        "summary": f"[MaiWill external_id={EXTERNAL_ID}] 标题"}],
                }
            if endpoint.endswith("paragraph-detail"):
                return {"success": True, "paragraph": {
                    "hash": MEMORY_ID, "source": SOURCE,
                    "content": f"[MaiWill external_id={EXTERNAL_ID}]\n主题\n\n知识内容",
                }}
            if endpoint.endswith("tasks/1234567890abcdef1234567890abcdef"):
                return {"success": True, "task": {"status": "completed", "files": [
                    {"status": "completed", "failed_chunks": 0, "chunks": [{"status": "completed"}]},
                ]}}
            raise AssertionError(endpoint)

        self.bridge._request = request
        result = self.bridge.create_memory_import(
            external_id=EXTERNAL_ID, title="主题", content="知识内容",
            source_type="mai_will.knowledge", tags=[],
            task_id="1234567890abcdef1234567890abcdef",
        )
        self.assertEqual(result["memory_id"], MEMORY_ID)
        self.assertFalse(any(call[1].endswith("import/paste") for call in calls))

    def test_missing_previous_task_retries_after_stable_memory_check(self) -> None:
        calls = []
        def request(method, endpoint, **kwargs):
            calls.append((method, endpoint))
            if endpoint.endswith("records/search"):
                return {"success": True, "items": []}
            if endpoint.endswith("tasks/1234567890abcdef1234567890abcdef"):
                return {"success": False, "task": None, "error": "任务不存在"}
            if endpoint.endswith("import/paste"):
                return {"success": True, "task": {"task_id": "fedcba0987654321fedcba0987654321", "status": "queued"}}
            raise AssertionError(endpoint)
        self.bridge._request = request
        def on_task(task_id):
            return True
        def immediate_wait(task_id, filename, external_id, *, initial=None, body_content=None):
            calls.append(("wait", task_id))
            return {"task_id": task_id, "memory_id": MEMORY_ID, "status": "completed"}
        self.bridge._wait_for_memory_import = immediate_wait
        result = self.bridge.create_memory_import(
            external_id=EXTERNAL_ID, title="主题", content="知识内容",
            source_type="mai_will.knowledge", tags=[],
            task_id="1234567890abcdef1234567890abcdef", on_task_created=on_task,
        )
        self.assertEqual(result["task_id"], "fedcba0987654321fedcba0987654321")
        self.assertTrue(any(endpoint.endswith("import/paste") for _, endpoint in calls))

    def test_import_task_creation_persists_task_id_for_retry(self) -> None:
        tasks = []
        def request(method, endpoint, **kwargs):
            if endpoint.endswith("records/search"):
                return {"success": True, "items": []}
            if endpoint.endswith("import/paste"):
                return {"success": True, "task": {"task_id": "1234567890abcdef1234567890abcdef", "status": "queued"}}
            if endpoint.endswith("tasks/1234567890abcdef1234567890abcdef"):
                raise TimeoutError("simulate lost task polling")
            raise AssertionError(endpoint)
        self.bridge._request = request
        with self.assertRaises(TimeoutError):
            self.bridge.create_memory_import(
                external_id=EXTERNAL_ID, title="主题", content="知识内容",
                source_type="mai_will.knowledge", tags=[],
                on_task_created=tasks.append,
            )
        self.assertEqual(tasks, ["1234567890abcdef1234567890abcdef"])

    def test_import_aborts_if_task_id_cannot_be_persisted(self) -> None:
        self.bridge._request = MagicMock(side_effect=[
            {"success": True, "items": []},
            {"success": True, "task": {"task_id": "1234567890abcdef1234567890abcdef", "status": "queued"}},
        ])
        with self.assertRaisesRegex(RuntimeError, "task_id"):
            self.bridge.create_memory_import(
                external_id=EXTERNAL_ID, title="主题", content="知识内容",
                source_type="mai_will.knowledge", tags=[], on_task_created=lambda task_id: False,
            )


if __name__ == "__main__":
    unittest.main()
