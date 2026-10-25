from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile
from unittest.mock import patch

from scripts.build_zip import build_zip


class BuildZipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project = Path(self.temp_dir.name)
        (self.project / "docs").mkdir()
        package = self.project / "maiwill"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.project / "_manifest.json").write_text(
            json.dumps({"id": "maibot-community.maiwill", "version": "0.1.0"}), encoding="utf-8"
        )
        (self.project / "plugin.py").write_text(
            "from .maiwill.runtime import VALUE\nENTRY = VALUE\n", encoding="utf-8"
        )
        (self.project / "webui.json").write_text(
            json.dumps({"schema_version": 2, "pages": [{"mode": "custom", "entry": "webui/conversation.html"}]}),
            encoding="utf-8",
        )
        (self.project / "webui").mkdir(exist_ok=True)
        (self.project / "webui" / "conversation.html").write_text("<p>Conversation</p>", encoding="utf-8")
        (self.project / "config.toml").write_text("[plugin]\nconfig_version = '0.1.0'\n", encoding="utf-8")
        (self.project / "docs" / "plugin-implementation.md").write_bytes(b"Plugin README\n")
        (self.project / "tests").mkdir()
        (self.project / "tests" / "test_runtime.py").write_text("DEV_ONLY = True\n", encoding="utf-8")
        (self.project / ".git").mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_archive_has_install_layout_and_excludes_development_files(self) -> None:
        archive_path = build_zip(self.project)

        with ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            self.assertEqual(
                names,
                {
                    "_manifest.json",
                    "plugin.py",
                    "webui.json",
                    "config.toml",
                    "README.md",
                    "maiwill/__init__.py",
                    "maiwill/runtime.py",
                    "webui/conversation.html",
                },
            )
            self.assertEqual(archive.read("README.md"), b"Plugin README\n")
            self.assertIsNone(archive.testzip())

    def test_archive_build_is_reproducible(self) -> None:
        archive_path = build_zip(self.project)
        first_build = archive_path.read_bytes()
        build_zip(self.project)
        self.assertEqual(archive_path.read_bytes(), first_build)

    def test_custom_page_is_packaged_and_missing_entry_is_rejected(self) -> None:
        (self.project / "webui").mkdir(exist_ok=True)
        html = self.project / "webui" / "conversation.html"
        html.write_text("<p>意识会话</p>", encoding="utf-8")
        declaration = {"schema_version": 2, "pages": [{"mode": "custom", "entry": "webui/conversation.html"}]}
        (self.project / "webui.json").write_text(json.dumps(declaration), encoding="utf-8")
        with ZipFile(build_zip(self.project)) as archive:
            self.assertEqual(archive.read("webui/conversation.html").decode("utf-8"), "<p>意识会话</p>")
        html.unlink()
        with self.assertRaises(FileNotFoundError):
            build_zip(self.project)

    def test_custom_page_outside_plugin_is_rejected(self) -> None:
        declaration = {"schema_version": 2, "pages": [{"mode": "custom", "entry": "../outside.html"}]}
        (self.project / "webui.json").write_text(json.dumps(declaration), encoding="utf-8")
        with self.assertRaises(ValueError):
            build_zip(self.project)

    def test_action_confirmation_over_80_characters_is_rejected(self) -> None:
        declaration = {
            "schema_version": 2,
            "pages": [{
                "mode": "declarative", "id": "records",
                "actions": {"sync_candidate": {"confirmation": "x" * 81}},
            }],
        }
        (self.project / "webui.json").write_text(json.dumps(declaration), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "confirmation exceeds 80 characters"):
            build_zip(self.project)

    def test_archive_imports_as_the_hosts_synthetic_plugin_package(self) -> None:
        archive_path = build_zip(self.project)
        with ZipFile(archive_path) as archive:
            archive.extractall(self.project / "installed")

        module_name = "_maibot_plugin_maibot_community_maiwill"
        plugin_path = self.project / "installed" / "plugin.py"
        spec = importlib.util.spec_from_file_location(
            module_name,
            plugin_path,
            submodule_search_locations=[str(plugin_path.parent)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {module_name: module}):
            spec.loader.exec_module(module)
            self.assertEqual(module.ENTRY, 1)

    def test_archive_build_rejects_package_symlinks(self) -> None:
        outside = self.project.parent / "outside.py"
        outside.write_text("SECRET = True\n", encoding="utf-8")
        link = self.project / "maiwill" / "external.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are unavailable in this environment")

        with self.assertRaises(ValueError):
            build_zip(self.project)


if __name__ == "__main__":
    unittest.main()
