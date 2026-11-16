"""Config version migration tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maa_analyzer.config import (
    CONFIG_VERSION,
    ConfigVersionError,
    default_config,
    load_config_file,
    migrate_config,
)
from tools.config.editor import default_document


class ConfigMigrationTests(unittest.TestCase):
    def test_default_configs_use_current_version(self):
        self.assertEqual(default_config()["version"], CONFIG_VERSION)
        self.assertEqual(default_document()["version"], CONFIG_VERSION)

    def test_missing_version_is_migrated_and_written_with_backup(self):
        original = {
            "onebot": {"mode": "http_ws"},
            "bot": {"configs": {"默认": {}}},
            "llm": {
                "providers": [{
                    "name": "default",
                    "base_url": "https://example.test/v1",
                    "models": [{"model": "model"}],
                }],
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(
                json.dumps(original, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            loaded = load_config_file(path)

            self.assertEqual(loaded["version"], CONFIG_VERSION)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                loaded,
            )
            backup = path.with_name("config.json.v1.bak")
            self.assertEqual(
                json.loads(backup.read_text(encoding="utf-8")),
                original,
            )

    def test_versioned_file_is_not_rewritten(self):
        cfg = default_config()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")

            with patch("maa_analyzer.config.migration.os.replace") as replace:
                loaded = load_config_file(path)

            replace.assert_not_called()
            self.assertEqual(loaded["version"], CONFIG_VERSION)

    def test_v1_legacy_llm_is_converted(self):
        cfg = {
            "bot": {
                "configs": {
                    "默认": {},
                    "备用": {
                        "llm": {
                            "models": [
                                {"name": "default", "model": "custom-model"}
                            ]
                        }
                    },
                }
            },
            "llm": {
                "base_url": "https://legacy.example/v1",
                "api_key": "secret",
                "model": "main-model",
                "fallback_models": ["backup-model"],
                "timeout_seconds": 42,
                "temperature": 0.2,
                "max_tokens": 4096,
                "max_context_tokens": 32000,
                "removed_field": "drop-me",
            },
        }

        migrated, changed = migrate_config(cfg)

        self.assertTrue(changed)
        self.assertEqual(migrated["version"], CONFIG_VERSION)
        llm = migrated["llm"]
        self.assertEqual(
            [model["model"] for model in llm["providers"][0]["models"]],
            ["main-model", "backup-model"],
        )
        self.assertEqual(
            llm["providers"][0]["base_url"],
            "https://legacy.example/v1",
        )
        self.assertEqual(llm["providers"][0]["api_key"], "secret")
        self.assertEqual(llm["providers"][0]["timeout_seconds"], 42)
        self.assertEqual(llm["defaults"]["max_tokens"], 4096)
        self.assertEqual(llm["defaults"]["max_context_tokens"], 32000)
        self.assertNotIn("temperature", llm)
        self.assertNotIn("removed_field", llm)
        self.assertEqual(
            migrated["bot"]["configs"]["默认"]["llm"]["models"],
            [
                {"name": "default", "model": "main-model"},
                {"name": "default", "model": "backup-model"},
            ],
        )
        self.assertEqual(
            migrated["bot"]["configs"]["备用"]["llm"]["models"],
            [{"name": "default", "model": "custom-model"}],
        )

    def test_v1_removed_section_fields_are_discarded(self):
        cfg = {
            "bot": {
                "configs": {
                    "默认": {
                        "settings": {
                            "max_report_images": 6,
                            "merge_messages": True,
                            "file_prefix": "legacy",
                            "removed_setting": "drop-me",
                        },
                        "project": {
                            "name": "MaaXXX",
                            "repo": {
                                "url": "https://example.test/repo",
                                "removed_repo_field": True,
                            },
                            "removed_project_field": True,
                        },
                    }
                }
            },
        }

        migrated, changed = migrate_config(cfg)

        self.assertTrue(changed)
        profile = migrated["bot"]["configs"]["默认"]
        self.assertEqual(profile["settings"], {"max_report_images": 6})
        self.assertEqual(
            profile["project"],
            {
                "name": "MaaXXX",
                "repo": {"url": "https://example.test/repo"},
            },
        )

    def test_migration_is_idempotent(self):
        migrated, _ = migrate_config(
            {
                "llm": {
                    "base_url": "https://legacy.example/v1",
                    "model": "main-model",
                }
            }
        )

        unchanged, changed = migrate_config(migrated)

        self.assertFalse(changed)
        self.assertEqual(unchanged, migrated)

    def test_invalid_or_future_versions_are_rejected(self):
        for value in (True, "2", 2.0, 0, -1, CONFIG_VERSION + 1):
            with self.subTest(value=value):
                with self.assertRaises(ConfigVersionError):
                    migrate_config({"version": value})


if __name__ == "__main__":
    unittest.main()
