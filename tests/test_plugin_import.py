from __future__ import annotations

import importlib.util
import asyncio
import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

from pydantic import BaseModel, Field
from maiwill.storage import Storage

from scripts.build_zip import PROJECT_ROOT, build_zip


class PluginImportTest(unittest.TestCase):
    def test_manifest_requests_global_config_read_for_persona(self) -> None:
        manifest = json.loads((PROJECT_ROOT / "_manifest.json").read_text(encoding="utf-8"))

        self.assertIn("config.get", manifest["capabilities"])

    def test_archive_loads_with_hosts_synthetic_package_name(self) -> None:
        archive_path = build_zip(PROJECT_ROOT)
        plugin_id = "maibot-community.maiwill"
        module_name = "_maibot_plugin_maibot_community_maiwill"

        sdk = ModuleType("maibot_sdk")
        sdk.__path__ = []
        sdk.API = lambda *args, **kwargs: lambda function: function
        sdk.Tool = lambda *args, **kwargs: lambda function: function
        sdk.MaiBotPlugin = type("MaiBotPlugin", (), {"__init__": lambda self: None})
        sdk.Field = Field
        sdk.PluginConfigBase = BaseModel
        sdk_types = ModuleType("maibot_sdk.types")
        sdk_types.ToolParameterInfo = lambda **kwargs: SimpleNamespace(**kwargs)
        sdk_types.ToolParamType = SimpleNamespace(STRING="string", ARRAY="array")

        with tempfile.TemporaryDirectory() as directory:
            with ZipFile(archive_path) as archive:
                archive.extractall(directory)
            plugin_path = Path(directory) / "plugin.py"
            spec = importlib.util.spec_from_file_location(
                module_name,
                plugin_path,
                submodule_search_locations=[str(plugin_path.parent)],
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            host_search_path = [
                entry
                for entry in sys.path
                if not Path(entry or ".").resolve().is_relative_to(PROJECT_ROOT)
            ]
            with patch.dict(
                sys.modules,
                {
                    "maibot_sdk": sdk,
                    "maibot_sdk.types": sdk_types,
                    module_name: module,
                },
            ), patch.object(sys, "path", host_search_path):
                spec.loader.exec_module(module)
                instance = module.create_plugin()
                self.assertIsInstance(instance, sdk.MaiBotPlugin)
                self.assertEqual(instance.__class__.__module__, module_name)
                self.assertEqual(module.MaiWillPlugin._format_dashboard_number(1_000_000), "1,000,000")
                self.assertEqual(module.MaiWillPlugin._format_dashboard_number(-12_345.5), "-12,345.5")
                self.assertEqual(
                    module.MaiWillPlugin._valid_usage(
                        {"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100}
                    ),
                    (90, 10, 100),
                )
                self.assertIsNone(
                    module.MaiWillPlugin._valid_usage(
                        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                    )
                )
                self.assertIsNone(module.MaiWillPlugin._valid_usage({"success": True}))

                database = Path(tempfile.mkdtemp()) / "maiwill.sqlite3"
                storage = Storage(database)
                now = time.time()
                storage.initialize_session(enabled=True, auto_start=True, now=now)
                storage.sync_resource_config(1_000_000, 100_000, start_at_capacity=True, now=now)
                instance._storage = storage
                instance._data_dir = database.parent
                instance.config = SimpleNamespace(
                    general=SimpleNamespace(
                        model_task="planner",
                        model_name="",
                        context_limit_tokens=258_000,
                        context_compression_threshold=0.8,
                    ),
                    resources=SimpleNamespace(
                        recovery_tokens_per_hour=100_000,
                        capacity_tokens=1_000_000,
                    ),
                    phone=SimpleNamespace(
                        adb_path="",
                        device_address="127.0.0.1:16384",
                    ),
                )
                configured_adb_path = database.parent / "adb.exe"
                configured_adb_path.write_bytes(b"fake test executable; never launched")
                capability_kwargs = []
                config_calls = []

                async def get_config(key, default=""):
                    config_calls.append(key)
                    return {
                        "bot.nickname": "测试麦麦",
                        "personality.personality": "喜欢探索和记录真实发现。",
                        "personality.behavior_style": "先观察，再做低风险的有益行动。",
                        "personality.reply_style": "面向用户回复简洁自然。",
                    }.get(key, default)

                instance.ctx = SimpleNamespace(
                    logger=SimpleNamespace(warning=lambda *args: None, info=lambda *args: None),
                    config=SimpleNamespace(get=get_config),
                )
                asyncio.run(instance._refresh_persona_reference())
                self.assertEqual(config_calls, [
                    "bot.nickname", "personality.personality",
                    "personality.behavior_style", "personality.reply_style",
                ])

                async def call_capability(*args, **kwargs):
                    capability_kwargs.append(kwargs)
                    if args[0] == "knowledge.search":
                        return {"success": True, "content": "相关记忆结果"}
                    return {
                        "success": True,
                        "response": '{"decision": "rest", "duration_seconds": 30}',
                        "prompt_tokens": 10_000,
                        "completion_tokens": 2_345,
                        "total_tokens": 12_345,
                        "model_name": "test-model",
                    }

                instance.ctx = SimpleNamespace(
                    call_capability=call_capability,
                    logger=SimpleNamespace(info=lambda *args: None, warning=lambda *args: None),
                    config=SimpleNamespace(get=get_config),
                )
                context = {}
                decision = asyncio.run(instance._request_decision("session-1", context, []))
                self.assertEqual(decision["decision"], "rest")
                self.assertAlmostEqual(context["resource"]["balance"], 987_655, delta=1)
                self.assertEqual(storage.get_status_snapshot()["usage_request_count"], 1)
                self.assertNotIn("max_tokens", capability_kwargs[0])
                self.assertEqual(capability_kwargs[0]["task_name"], "planner")
                self.assertNotIn("model_name", capability_kwargs[0])
                prompt_text = capability_kwargs[0]["prompt"]
                self.assertTrue(prompt_text.startswith("【麦麦人设参考｜来自 MaiBot 当前配置】"))
                self.assertIn("人格设定：喜欢探索和记录真实发现。", prompt_text)
                self.assertIn("行为风格：先观察，再做低风险的有益行动。", prompt_text)
                self.assertIn("表达风格：面向用户回复简洁自然。", prompt_text)
                self.assertIn("没有 active 目标绝不是休息理由", prompt_text)
                self.assertIn("只有确实找不到具体、安全且有价值的发现步骤", prompt_text)
                self.assertIn("无事可做时不要选择 finish", prompt_text)
                self.assertIn("用户已请求停止", prompt_text)
                self.assertTrue(json.loads((PROJECT_ROOT / "webui.json").read_text(encoding="utf-8"))["pages"][0]["actions"]["force_stop_session"]["confirmation"].startswith("强制停止会立即中断"))
                initial_payload = json.loads(prompt_text.split("输入数据：\n", 1)[1])
                initial_available = initial_payload["available_capabilities"]
                self.assertEqual(
                    [item["kind"] for item in initial_payload["exploration_options"]],
                    ["self_directed_discovery"],
                )
                self.assertNotIn("memory.search", [item["name"] for item in initial_available])
                self.assertIn("diary.write", prompt_text)
                self.assertIn("reflection.create", prompt_text)
                available = json.loads(prompt_text.split("输入数据：\n", 1)[1])["available_capabilities"]
                self.assertNotIn("phone.use", [item["name"] for item in available])
                self.assertNotIn("chat.proactive", [item["name"] for item in available])

                # The assembled prompt is checked before the next request, so a sudden
                # large capability result cannot push it past the configured target.
                instance.config.general.context_limit_tokens = 2048
                instance.config.general.context_compression_threshold = 0.9
                long_context = {"messages": [{"role": "observation", "text": "旧历史" * 400}]}
                asyncio.run(instance._request_decision("session-1", long_context, []))
                self.assertNotIn("旧历史", capability_kwargs[-1]["prompt"])
                self.assertEqual(long_context["messages"][0]["role"], "history_compacted")
                self.assertGreater(long_context["messages"][0]["discarded_message_count"], 0)

                # Even with recent history below the local estimate threshold, a
                # high host-reported usage must compact it before later decisions.
                capability_kwargs.clear()

                async def high_usage_call(capability, **kwargs):
                    capability_kwargs.append(kwargs)
                    return {
                        "success": True,
                        "response": '{"decision": "rest", "duration_seconds": 30}',
                        "prompt_tokens": 76000,
                        "completion_tokens": 1,
                        "total_tokens": 76001,
                        "model_name": "test-model",
                    }

                instance.ctx = SimpleNamespace(call_capability=high_usage_call)
                instance.config.general.context_limit_tokens = 81920
                instance.config.general.context_compression_threshold = 0.9
                actual_usage_context = {
                    "messages": [{"role": "observation", "text": "recent " * 30000}]
                }
                asyncio.run(instance._request_decision("session-1", actual_usage_context, []))
                self.assertIn("recent ", capability_kwargs[-1]["prompt"])
                self.assertEqual(actual_usage_context["messages"][0]["role"], "history_compacted")
                next_context = {"messages": actual_usage_context["messages"]}
                instance.ctx = SimpleNamespace(
                    call_capability=call_capability,
                    logger=SimpleNamespace(info=lambda *args: None, warning=lambda *args: None),
                    config=SimpleNamespace(get=get_config),
                )
                asyncio.run(instance._request_decision("session-1", next_context, []))
                self.assertNotIn("recent ", capability_kwargs[-1]["prompt"])
                instance.ctx = SimpleNamespace(call_capability=call_capability)
                instance.config.general.context_limit_tokens = 258_000
                instance.config.general.context_compression_threshold = 0.8

                # The screenshot crosses the public LLM API only as a transient
                # image part; durable context keeps its path and attempt count.
                screenshot_path = "phone/screenshots/0123456789abcdef0123456789abcdef.png"
                screenshot_file = database.parent / screenshot_path
                screenshot_file.parent.mkdir(parents=True, exist_ok=True)
                ihdr = (320).to_bytes(4, "big") + (240).to_bytes(4, "big") + bytes((8, 2, 0, 0, 0))
                screenshot_file.write_bytes(
                    b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + ihdr + b"\0\0\0\0"
                )
                vision_context = {"messages": [{
                    "role": "capability_result",
                    "capability": "phone.use",
                    "result": {"success": True, "status": "screenshot", "path": screenshot_path},
                }]}
                asyncio.run(instance._request_decision("session-1", vision_context, []))
                vision_prompt = capability_kwargs[-1]["prompt"]
                self.assertIsInstance(vision_prompt, list)
                image_parts = vision_prompt[0]["content"]
                self.assertEqual(image_parts[2]["type"], "image")
                self.assertEqual(image_parts[2]["image_format"], "png")
                self.assertEqual(base64.b64decode(image_parts[2]["image_base64"]), screenshot_file.read_bytes())
                storage.save_session_context(vision_context)
                saved_context = storage.get_session()["context"]
                self.assertEqual(saved_context["messages"][0]["result"]["status"], "visual_input_sent")
                self.assertNotIn("path", saved_context["messages"][0]["result"])
                self.assertNotIn("image_base64", json.dumps(saved_context))
                next_vision_context = storage.get_session()["context"]
                asyncio.run(instance._request_decision("session-1", next_vision_context, []))
                self.assertIsInstance(capability_kwargs[-1]["prompt"], str)

                instance.config.phone.adb_path = str(configured_adb_path)
                configured_phone_context = {}
                asyncio.run(instance._request_decision("session-1", configured_phone_context, []))
                configured_prompt = capability_kwargs[-1]["prompt"]
                configured_available = json.loads(
                    configured_prompt.split("输入数据：\n", 1)[1]
                )["available_capabilities"]
                self.assertIn("phone.use", [item["name"] for item in configured_available])
                configured_options = json.loads(configured_prompt.split("输入数据：\n", 1)[1])["exploration_options"]
                self.assertEqual(configured_options[0]["kind"], "self_directed_discovery")
                self.assertEqual(configured_options[0]["available_capabilities"], ["phone.use"])
                instance.config.phone.adb_path = ""

                storage.record_interest(
                    "潮汐", "之后想继续聊", "follow_up", [],
                    source_stream_id="stream-allowed", external_id="interest-proactive-test",
                )
                interest_context = {}
                interest_events = storage.get_pending_events()
                asyncio.run(instance._request_decision("session-1", interest_context, interest_events))
                interest_prompt = capability_kwargs[-1]["prompt"]
                interest_payload = json.loads(interest_prompt.split("输入数据：\n", 1)[1])
                self.assertIn("chat.proactive", [item["name"] for item in interest_payload["available_capabilities"]])
                self.assertEqual(
                    interest_payload["recent_interests"],
                    [{"external_id": "interest-proactive-test", "source_stream_id": "stream-allowed",
                      "topic": "潮汐", "reason": "之后想继续聊"}],
                )
                self.assertIn("exploration.goal.create", [item["name"] for item in interest_payload["available_capabilities"]])
                self.assertIn("exploration.goal.update", [item["name"] for item in interest_payload["available_capabilities"]])
                self.assertIn("recent_successful_actions", interest_payload)
                self.assertTrue(any(
                    item["kind"] == "follow_recorded_interest"
                    for item in interest_payload["exploration_options"]
                ))
                candidate_for_goal = storage.create_learning_candidate(
                    "mai_will.knowledge", "潮汐知识", "候选内容", [],
                    origin_session_id="session-1", source_ref="source-goal",
                )
                learning_options = module.build_exploration_options(
                    available_capabilities=interest_payload["available_capabilities"],
                    learning_candidates=storage.get_learning_candidates(limit=10),
                    recent_interests=interest_payload["recent_interests"],
                    exploration_goals=[], recent_actions=[], context_message_count=0,
                )
                self.assertTrue(any(
                    item["kind"] == "review_learning_candidates"
                    for item in learning_options
                ))
                instance._storage = storage
                created_goal = asyncio.run(instance._create_exploration_goal({
                    "title": "了解潮汐", "topic": "潮汐", "objective": "整理潮汐知识",
                    "source_type": "learning_candidate", "source_ref": candidate_for_goal["external_id"],
                    "evidence_summary": "由本地学习候选产生", "next_step": "查阅已有资料",
                }))
                self.assertTrue(created_goal["success"])
                goal_id = created_goal["goal"]["goal_id"]
                rejected_goal_progress = asyncio.run(instance._update_exploration_goal({
                    "goal_id": goal_id, "action": "progress", "note": "已完成研究",
                }))
                self.assertFalse(rejected_goal_progress["success"])
                goal_listing = asyncio.run(instance._list_exploration_goals({"limit": 10}))
                self.assertEqual(goal_listing["goals"][0]["goal_id"], goal_id)
                rejected_replan = asyncio.run(instance._update_exploration_goal({
                    "goal_id": goal_id, "action": "replan", "note": "当前路径受阻",
                }))
                self.assertFalse(rejected_replan["success"])
                replanned_goal = asyncio.run(instance._update_exploration_goal({
                    "goal_id": goal_id, "action": "replan", "note": "候选资料不足",
                    "next_step": "先检索相邻主题资料",
                }))
                self.assertTrue(replanned_goal["success"])
                self.assertEqual(replanned_goal["goal"]["next_step"], "先检索相邻主题资料")
                self.assertEqual(replanned_goal["goal"]["progress"][-1]["action"], "replanned")
                self.assertIn(
                    "memory.search",
                    [item["name"] for item in interest_payload["available_capabilities"]],
                )
                self.assertEqual(
                    [item["name"] for item in interest_payload["available_capabilities"]].count("memory.search"),
                    1,
                )
                prior_calls = len(capability_kwargs)
                rejected_proactive = asyncio.run(
                    instance._send_proactive_message(
                        {
                            "interest_id": "interest-proactive-test",
                            "stream_id": "stream-unrelated",
                            "intent": "分享观察",
                        }
                    )
                )
                self.assertFalse(rejected_proactive["success"])
                self.assertEqual(len(capability_kwargs), prior_calls)
                proactive_result = asyncio.run(
                    instance._send_proactive_message(
                        {
                            "interest_id": "interest-proactive-test",
                            "stream_id": "stream-allowed",
                            "intent": "分享我刚观察到的潮汐现象",
                            "reason": "你之前记录过对潮汐的兴趣",
                        }
                    )
                )
                self.assertTrue(proactive_result["success"])
                self.assertEqual(proactive_result["status"], "queued")
                self.assertEqual(capability_kwargs[-1]["stream_id"], "stream-allowed")
                self.assertEqual(capability_kwargs[-1]["intent"], "分享我刚观察到的潮汐现象")
                proactive_action = storage.get_status_snapshot()["recent_actions"][0]
                self.assertEqual(proactive_action["capability"], "chat.proactive")
                self.assertEqual(proactive_action["status"], "completed")
                proactive_call_count = len(capability_kwargs)
                repeated_proactive = asyncio.run(
                    instance._send_proactive_message(
                        {
                            "interest_id": "interest-proactive-test",
                            "stream_id": "stream-allowed",
                            "intent": "分享我刚观察到的潮汐现象",
                            "reason": "你之前记录过对潮汐的兴趣",
                        }
                    )
                )
                self.assertEqual(repeated_proactive, proactive_result)
                self.assertEqual(len(capability_kwargs), proactive_call_count)

                instance.will_status = module.MaiWillPlugin.will_status.__get__(instance)
                status = asyncio.run(instance.will_status())
                self.assertNotIn("context", status["session"])
                self.assertEqual(status["session"]["status_display"], "活动中")
                self.assertLess(status["resource"]["balance_percent"], 100.0)
                self.assertIn(",", status["resource"]["balance_display"])
                self.assertTrue(status["resource"]["balance_percent_display"].endswith("%"))
                self.assertEqual(status["open_learning_candidate_count"], 1)
                self.assertEqual(status["exploration_goals"][0]["goal_id"], goal_id)
                self.assertIn("updated_at_utc", status["exploration_goals"][0])
                self.assertIn("last_progress_at_utc", status["exploration_goals"][0])
                self.assertEqual(status["exploration_timeline"][0]["action"], "replanned")
                self.assertIn(
                    "created", [item["action"] for item in status["exploration_timeline"]]
                )
                self.assertEqual(status["resource"]["capacity_display"], "1,000,000")
                self.assertIn("open_learning_candidate_count", status)
                self.assertEqual(status["resource"]["recovery_per_hour_display"], "100,000")
                self.assertIn(",", status["resource"]["spent_tokens_display"])
                webui = json.loads((PROJECT_ROOT / "webui.json").read_text(encoding="utf-8"))
                overview = next(page for page in webui["pages"] if page["id"] == "overview")
                stat_fields = {
                    item["value"]["field"]
                    for item in overview["content"][0]["children"]
                    if item.get("type") == "stat"
                }
                self.assertIn("session.status_display", stat_fields)
                self.assertEqual(
                    {item["action"] for item in overview["content"] if item.get("type") == "button" and "action" in item},
                    {"start_session", "request_session_stop", "force_stop_session", "restore_capacity"},
                )
                self.assertIn("未知结果", webui["pages"][0]["actions"]["force_stop_session"]["confirmation"])
                self.assertIn("resource.balance_display", stat_fields)
                self.assertIn("resource.balance_percent_display", stat_fields)
                dashboard_goal_table = next(
                    item for item in overview["content"]
                    if item.get("type") == "card" and item.get("label") == "当前与近期探索目标"
                )["children"][0]
                self.assertEqual(dashboard_goal_table["value"]["field"], "exploration_goals")
                self.assertLessEqual(len(overview["content"][0]["children"]), 4)
                dashboard_labels = {item.get("label") for item in overview["content"]}
                self.assertNotIn("体力账本", dashboard_labels)
                self.assertNotIn("探索目标进展时间线", dashboard_labels)
                self.assertNotIn("最近本地学习候选（尚未同步）", dashboard_labels)
                storage.set_session_status("DISCONNECTED")
                disconnected_status = asyncio.run(instance.will_status())
                self.assertEqual(disconnected_status["session"]["status_display"], "重连等待中")
                for item in overview["content"][0]["children"]:
                    if item.get("type") == "stat":
                        self.assertLessEqual(len(item["label"]), 80)
                memory_result = asyncio.run(
                    instance._search_memory({
                        "interest_id": "interest-proactive-test",
                        "query": "潮汐",
                        "limit": 3,
                    })
                )
                self.assertEqual(memory_result, {"success": True, "content": "相关记忆结果"})
                self.assertEqual(capability_kwargs[-1]["query"], "潮汐")
                self.assertEqual(capability_kwargs[-1]["limit"], 3)
                self.assertEqual(capability_kwargs[-1]["mode"], "search")
                self.assertEqual(capability_kwargs[-1]["chat_id"], "stream-allowed")
                self.assertTrue(capability_kwargs[-1]["respect_filter"])
                prior_calls = len(capability_kwargs)
                rejected_memory_scope = asyncio.run(
                    instance._search_memory({
                        "interest_id": "unrelated-interest",
                        "query": "潮汐",
                        "limit": 3,
                    })
                )
                self.assertFalse(rejected_memory_scope["success"])
                self.assertEqual(len(capability_kwargs), prior_calls)
                invalid = asyncio.run(
                    instance._search_memory({
                        "interest_id": "interest-proactive-test",
                        "query": "",
                        "limit": 3,
                    })
                )
                self.assertFalse(invalid["success"])
                self.assertEqual(len(capability_kwargs), prior_calls)
                invalid_limit = asyncio.run(
                    instance._search_memory({
                        "interest_id": "interest-proactive-test",
                        "query": "潮汐",
                        "limit": 11,
                    })
                )
                self.assertFalse(invalid_limit["success"])
                self.assertEqual(len(capability_kwargs), prior_calls)

                diary_args = {"title": "关于潮汐的思考", "content": "今天继续观察海潮。"}
                diary_result = asyncio.run(instance._write_diary(diary_args))
                self.assertTrue(diary_result["success"])
                diary_path = database.parent / diary_result["path"]
                self.assertTrue(diary_path.is_file())
                self.assertIn("今天继续观察海潮。", diary_path.read_text(encoding="utf-8"))
                duplicate_diary = asyncio.run(instance._write_diary(diary_args))
                self.assertEqual(duplicate_diary, diary_result)
                self.assertEqual(len(list((database.parent / "diary").glob("*.md"))), 1)
                outside_dir = database.parent.parent / (database.parent.name + "-outside")
                outside_dir.mkdir()
                redirected_dir = database.parent / "diary"
                redirected_dir.rename(database.parent / "diary-original")
                try:
                    try:
                        redirected_dir.symlink_to(outside_dir, target_is_directory=True)
                    except (OSError, NotImplementedError):
                        (database.parent / "diary-original").rename(redirected_dir)
                    else:
                        with self.assertRaises(ValueError):
                            module.MaiWillPlugin._write_diary_entry(
                                database.parent, "外部跳转", "不可写到数据目录外"
                            )
                        redirected_dir.unlink()
                        (database.parent / "diary-original").rename(redirected_dir)
                finally:
                    if redirected_dir.is_symlink():
                        redirected_dir.unlink()
                    if (database.parent / "diary-original").exists():
                        (database.parent / "diary-original").rename(redirected_dir)
                bad_diary = asyncio.run(
                    instance._write_diary({"title": " ", "content": "x"})
                )
                self.assertFalse(bad_diary["success"])

                reflection_args = {
                    "source_type": "mai_will.knowledge",
                    "title": "潮汐与月球",
                    "content": "月球引潮力是潮汐的重要成因。",
                    "tags": ["ocean", "science"],
                    "source_ref": "interest-proactive-test",
                    "confidence": 0.92,
                }
                reflection = asyncio.run(instance._create_reflection(reflection_args))
                self.assertTrue(reflection["success"])
                self.assertTrue(reflection["external_id"].startswith("mai-will:learning:sha256:"))
                self.assertIn("尚未同步", reflection["summary"])
                duplicate_reflection = asyncio.run(instance._create_reflection(reflection_args))
                self.assertFalse(duplicate_reflection["created"])
                self.assertEqual(duplicate_reflection["external_id"], reflection["external_id"])
                updated_reflection = asyncio.run(
                    instance._update_reflection(
                        {
                            "external_id": reflection["external_id"],
                            "status": "resolved",
                            "note": "等待宿主写入能力",
                        }
                    )
                )
                self.assertTrue(updated_reflection["success"])
                self.assertEqual(updated_reflection["status"], "resolved")
                self.assertIn("尚未同步", updated_reflection["summary"])
                self.assertEqual(storage.get_status_snapshot()["open_learning_candidate_count"], 1)
                listed_reflections = asyncio.run(
                    instance._list_reflections({"status": "resolved", "limit": 5})
                )
                self.assertTrue(listed_reflections["success"])
                self.assertEqual(listed_reflections["count"], 1)
                self.assertEqual(listed_reflections["memory_sync_status"], "not_synced")
                self.assertLessEqual(
                    len(listed_reflections["candidates"][0]["content_preview"]),
                    1000,
                )
                deletion = asyncio.run(
                    instance.will_delete_learning_candidate(reflection["external_id"])
                )
                self.assertTrue(deletion["success"])
                self.assertTrue(deletion["deleted"])
                self.assertFalse(
                    asyncio.run(
                        instance.will_delete_learning_candidate(reflection["external_id"])
                    )["success"]
                )
                self.assertEqual(
                    asyncio.run(instance._list_reflections({"limit": 5}))["count"],
                    1,
                )

                # Task selection and an explicit model must survive the packaged
                # plugin boundary without requiring a host-specific MaiWill task.
                instance.ctx = SimpleNamespace(
                    call_capability=call_capability,
                    logger=SimpleNamespace(info=lambda *args: None, warning=lambda *args: None),
                    config=SimpleNamespace(get=get_config),
                )
                for task in ("replyer", "utils", "memory", "mid_memory", "learner", "fast_model", "emoji", "vlm"):
                    with self.subTest(model_task=task):
                        instance.config.general.model_task = task
                        asyncio.run(instance._request_decision("session-1", {}, []))
                        self.assertEqual(capability_kwargs[-1]["task_name"], task)
                        self.assertNotIn("model_name", capability_kwargs[-1])
                instance.config.general.model_task = "replyer"
                instance.config.general.model_name = "will-model"
                asyncio.run(instance._request_decision("session-1", {}, []))
                self.assertEqual(capability_kwargs[-1]["task_name"], "replyer")
                self.assertEqual(capability_kwargs[-1]["model_name"], "will-model")

                failed_requests = []

                async def missing_model(capability, **kwargs):
                    failed_requests.append((capability, kwargs))
                    return {"success": False, "error": "未找到模型 'will-model' 的配置"}

                instance.ctx = SimpleNamespace(call_capability=missing_model)
                with self.assertRaisesRegex(RuntimeError, "未找到模型"):
                    asyncio.run(instance._request_decision("session-1", {}, []))
                self.assertEqual(len(failed_requests), 1)
                self.assertEqual(failed_requests[0][1]["task_name"], "replyer")
                self.assertEqual(failed_requests[0][1]["model_name"], "will-model")

                # The packaged page reaches the same persistent queue through
                # static plugin APIs; no host internals or extra LLM is involved.
                instance._runtime = module.SessionRuntime(storage, enabled=True)
                sent = asyncio.run(instance.will_send_message("请先保存观察。", "webui-test-message-0001"))
                self.assertTrue(sent["created"])
                repeated = asyncio.run(instance.will_send_message("请先保存观察。", "webui-test-message-0001"))
                self.assertFalse(repeated["created"])
                storage.save_session_context({"messages": [{"role": "observation", "note": "内部上下文示例"}]})
                conversation = asyncio.run(instance.will_conversation())
                self.assertEqual(conversation["messages"][-1]["content"], "请先保存观察。")
                self.assertNotIn("context", conversation["session"])
                self.assertEqual(conversation["context_summary"]["message_count"], 1)
                self.assertGreater(conversation["context_summary"]["estimated_tokens"], 0)
                self.assertNotIn("messages", conversation["context_summary"])
                self.assertNotIn("内部上下文示例", json.dumps(conversation["context_summary"], ensure_ascii=False))
                self.assertNotIn("will_current_context", module.MaiWillPlugin.__dict__)
                conversation_page = next(page for page in webui["pages"] if page["id"] == "conversation")
                self.assertEqual(conversation_page["queries"]["conversation"]["api"], "will_conversation")
                self.assertEqual(conversation_page["actions"]["send_message"]["api"], "will_send_message")
                self.assertTrue((Path(directory) / conversation_page["entry"]).is_file())
                self.assertIn("bot", instance.config_reload_subscriptions)
                config_calls.clear()
                instance.ctx = SimpleNamespace(
                    logger=SimpleNamespace(info=lambda *args: None, warning=lambda *args: None),
                    config=SimpleNamespace(get=get_config),
                )
                asyncio.run(instance.on_config_update("bot", {}, "test"))
                self.assertEqual(len(config_calls), 4)
                self.assertIn("人格设定：喜欢探索和记录真实发现。", instance._persona_reference)
                dashboard = next(page for page in webui["pages"] if page["id"] == "overview")
                self.assertLessEqual(len(dashboard["content"][0]["children"]), 4)
                dashboard_labels = {item.get("label") for item in dashboard["content"]}
                self.assertNotIn("体力账本", dashboard_labels)
                self.assertNotIn("探索目标进展时间线", dashboard_labels)
                self.assertNotIn("最近本地学习候选（尚未同步）", dashboard_labels)
                page_html = (Path(directory) / conversation_page["entry"]).read_text(encoding="utf-8")
                self.assertIn("当前会话上下文估算", page_html)
                self.assertIn("context_summary", page_html)


if __name__ == "__main__":
    unittest.main()
