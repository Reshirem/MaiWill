from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path
from types import ModuleType

from pydantic import BaseModel, Field

if "maibot_sdk" not in sys.modules:
    sdk = ModuleType("maibot_sdk")
    sdk.Field = Field
    sdk.PluginConfigBase = BaseModel
    sys.modules["maibot_sdk"] = sdk

from maiwill.config import MaiWillConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigFileTest(unittest.TestCase):
    def test_default_toml_has_host_required_plugin_section(self) -> None:
        with (PROJECT_ROOT / "config.toml").open("rb") as handle:
            config = tomllib.load(handle)

        self.assertEqual(config["plugin"]["config_version"], "0.1.35")
        self.assertIs(config["plugin"]["enabled"], True)
        validated = MaiWillConfig.model_validate(config)
        self.assertEqual(validated.plugin.config_version, "0.1.35")
        self.assertEqual(validated.general.idle_wait_seconds, 3600)
        self.assertEqual(validated.general.model_task, "planner")
        self.assertEqual(validated.general.model_name, "")
        self.assertEqual(validated.general.context_limit_tokens, 258_000)
        self.assertEqual(validated.general.context_compression_threshold, 0.8)
        self.assertFalse(validated.general.memory_bridge_enabled)
        self.assertEqual(validated.general.memory_webui_url, "http://127.0.0.1:8001")
        self.assertEqual(validated.resources.capacity_tokens, 10_000_000)
        self.assertEqual(validated.resources.recovery_tokens_per_hour, 1_000_000)
        self.assertTrue(validated.resources.start_at_capacity)
        self.assertEqual(validated.phone.device_address, "127.0.0.1:16384")
        self.assertEqual(validated.phone.adb_path, "")

    def test_model_version_lives_under_plugin_section(self) -> None:
        defaults = MaiWillConfig().model_dump()

        self.assertEqual(defaults["plugin"]["config_version"], "0.1.35")
        self.assertNotIn("config_version", defaults)
        self.assertFalse(defaults["general"]["memory_bridge_enabled"])

    def test_existing_config_keeps_user_settings_and_defaults_to_planner(self) -> None:
        config = MaiWillConfig.model_validate({
            "plugin": {"enabled": False, "config_version": "0.1.13"},
            "general": {"reconnect_initial_seconds": 12},
            "resources": {"capacity_tokens": 45000},
            "phone": {"device_address": "127.0.0.1:5555"},
        })
        self.assertEqual(config.general.model_task, "planner")
        self.assertEqual(config.general.model_name, "")
        self.assertEqual(config.general.context_limit_tokens, 258_000)
        self.assertEqual(config.general.context_compression_threshold, 0.8)
        self.assertFalse(config.plugin.enabled)
        self.assertEqual(config.general.reconnect_initial_seconds, 12)
        self.assertEqual(config.resources.capacity_tokens, 45000)
        self.assertEqual(config.phone.device_address, "127.0.0.1:5555")

    def test_model_selection_accepts_generation_tasks_and_normalizes_override(self) -> None:
        for task in ("planner", "replyer", "utils", "memory", "mid_memory", "learner", "fast_model", "emoji", "vlm"):
            with self.subTest(task=task):
                config = MaiWillConfig.model_validate({"general": {
                    "model_task": task,
                    "model_name": "  will-model  ",
                }})
                self.assertEqual(config.general.model_task, task)
                self.assertEqual(config.general.model_name, "will-model")
        self.assertEqual(MaiWillConfig.model_validate({"general": {"model_name": "  "}}).general.model_name, "")
        for task in ("maiwill", "embedding", "image_embedding", "voice", "web_search", "missing-task"):
            with self.subTest(task=task), self.assertRaises(ValueError):
                MaiWillConfig.model_validate({"general": {"model_task": task}})

    def test_context_management_settings_are_configurable_and_bounded(self) -> None:
        config = MaiWillConfig.model_validate({
            "general": {
                "context_limit_tokens": 128_000,
                "context_compression_threshold": 0.75,
            }
        })
        self.assertEqual(config.general.context_limit_tokens, 128_000)
        self.assertEqual(config.general.context_compression_threshold, 0.75)
        for value in (0, 1.01):
            with self.subTest(threshold=value), self.assertRaises(ValueError):
                MaiWillConfig.model_validate({"general": {"context_compression_threshold": value}})
        with self.assertRaises(ValueError):
            MaiWillConfig.model_validate({"general": {"context_limit_tokens": 100}})

    def test_memory_bridge_defaults_off_and_accepts_loopback_only(self) -> None:
        configured = MaiWillConfig.model_validate({"general": {
            "memory_bridge_enabled": True,
            "memory_webui_url": "http://[::1]:8123/",
        }})
        self.assertTrue(configured.general.memory_bridge_enabled)
        self.assertEqual(configured.general.memory_webui_url, "http://[::1]:8123")
        for url in ("http://example.com:8001", "http://127.0.0.1:8001/api", "http://localhost:8001/?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                MaiWillConfig.model_validate({"general": {"memory_webui_url": url}})

    def test_fields_keep_chinese_webui_metadata(self) -> None:
        plugin_config = MaiWillConfig.model_fields["plugin"].annotation
        resources_config = MaiWillConfig.model_fields["resources"].annotation
        general_config = MaiWillConfig.model_fields["general"].annotation
        enabled = plugin_config.model_fields["enabled"]
        capacity = resources_config.model_fields["capacity_tokens"]
        context_limit = general_config.model_fields["context_limit_tokens"]
        compression_threshold = general_config.model_fields["context_compression_threshold"]
        memory_bridge_enabled = general_config.model_fields["memory_bridge_enabled"]
        memory_webui_url = general_config.model_fields["memory_webui_url"]
        idle_wait = general_config.model_fields["idle_wait_seconds"]
        config_version = plugin_config.model_fields["config_version"]

        self.assertEqual(enabled.json_schema_extra["label"], "启用 MaiWill")
        self.assertIn("hint", enabled.json_schema_extra)
        self.assertEqual(capacity.json_schema_extra["label"], "体力上限（token）")
        self.assertEqual(context_limit.default, 258_000)
        self.assertEqual(compression_threshold.default, 0.8)
        self.assertFalse(memory_bridge_enabled.default)
        self.assertIn("管理员", memory_bridge_enabled.description)
        self.assertIn("回环", memory_webui_url.description)
        self.assertNotIn("decision_max_output_tokens", resources_config.model_fields)
        self.assertNotIn("single_call_limit_tokens", resources_config.model_fields)
        self.assertTrue(config_version.json_schema_extra["hidden"])

        for section_name in ("plugin", "general", "resources", "phone"):
            section_config = MaiWillConfig.model_fields[section_name].annotation
            self.assertTrue(section_config.__ui_label__)
            for field in section_config.model_fields.values():
                metadata = field.json_schema_extra or {}
                if metadata.get("hidden"):
                    continue
                self.assertTrue(metadata.get("label"), f"{section_name} field is missing a Chinese label")
                self.assertTrue(metadata.get("hint"), f"{section_name} field is missing an explanation")

    def test_phone_address_is_normalized_and_must_be_an_ip_with_valid_port(self) -> None:
        defaults = MaiWillConfig()
        self.assertEqual(defaults.phone.device_address, "127.0.0.1:16384")
        configured = MaiWillConfig.model_validate(
            {"phone": {"device_address": "[2001:db8::1]:5555"}}
        )
        self.assertEqual(configured.phone.device_address, "[2001:db8::1]:5555")
        for address in ("localhost:5555", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:abc"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                MaiWillConfig.model_validate({"phone": {"device_address": address}})


if __name__ == "__main__":
    unittest.main()
