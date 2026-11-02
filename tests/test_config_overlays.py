"""Config inheritance and AstrBot panel override tests."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from maa_analyzer.config import (
    PANEL_DEFAULTS,
    apply_panel_overrides,
    default_config,
    load_effective_config,
)


class PanelOverrideTests(unittest.TestCase):
    def _config(self) -> dict:
        cfg = default_config()
        configs = cfg["bot"]["configs"]
        configs["Alpha"] = configs.pop("默认")
        configs["Alpha"]["project"]["custom_advanced"] = {"keep": True}
        configs["Beta"] = copy.deepcopy(configs["Alpha"])
        configs["Beta"]["listen_groups"] = ["999"]
        configs["Beta"]["project"]["name"] = "BetaProject"
        configs["Beta"]["llm"]["models"] = [
            {"name": "beta", "model": "beta-model"},
            {"name": "backup", "model": "backup-model"},
        ]
        configs["Beta"]["settings"]["send_files"] = False
        return cfg

    def test_panel_only_overrides_selected_profile_and_preserves_advanced_json(self):
        cfg = self._config()
        panel = {
            "enabled_mode": "enabled",
            "default_config": "Beta",
            "listen_groups": ["10001", " 10002 ", ""],
            "notify_group": "10003",
            "command_prefix": "!maa",
            "owners": ["1", " 2 ", ""],
            "admins": ["3"],
            "report_mode": "image",
            "project_name": " Beta Project ",
            "archive_match_mode": "any",
            "repo_mode": "enabled",
            "llm_provider": "default",
            "llm_model": "gpt-4o-mini",
            "vision_mode": "disabled",
            "followup_mode": "disabled",
            "history_mode": "disabled",
            "commands_mode": "disabled",
        }

        result = apply_panel_overrides(cfg, panel)

        self.assertEqual(result["bot"]["configs"]["Alpha"], cfg["bot"]["configs"]["Alpha"])
        beta = result["bot"]["configs"]["Beta"]
        self.assertEqual(beta["listen_groups"], ["10001", "10002"])
        self.assertEqual(beta["notify_group"], "10003")
        self.assertIs(beta["settings"]["enabled"], True)
        self.assertIs(beta["settings"]["send_files"], False)
        self.assertEqual(beta["commands"]["prefix"], "!maa")
        self.assertEqual(beta["commands"]["owners"], ["1", "2"])
        self.assertEqual(beta["commands"]["admins"], ["3"])
        self.assertEqual(beta["report"]["render"], "image")
        self.assertEqual(beta["project"]["name"], "Beta Project")
        self.assertEqual(beta["project"]["archive_match_mode"], "any")
        self.assertIs(beta["project"]["repo"]["enabled"], True)
        self.assertEqual(
            beta["llm"]["models"],
            [
                {"name": "default", "model": "gpt-4o-mini"},
                {"name": "backup", "model": "backup-model"},
            ],
        )
        self.assertIs(beta["vision"]["enabled"], False)
        self.assertIs(beta["settings"]["followup_enabled"], False)
        self.assertIs(beta["settings"]["history_enabled"], False)
        self.assertIs(beta["commands"]["enabled"], False)
        self.assertEqual(
            beta["project"]["custom_advanced"],
            {"keep": True},
        )

    def test_schema_defaults_are_inheritance_markers(self):
        cfg = self._config()
        panel = dict(PANEL_DEFAULTS)

        self.assertEqual(apply_panel_overrides(cfg, panel), cfg)

    def test_string_and_list_panel_values_are_trimmed_when_applied(self):
        cfg = self._config()
        panel = {
            "default_config": " Alpha ",
            "data_dir": " runtime ",
            "notify_group": " 10003 ",
            "log_level": "warning",
        }

        result = apply_panel_overrides(cfg, panel)

        self.assertEqual(result["bot"]["data_dir"], "runtime")
        self.assertEqual(result["log"]["log_level"], "WARNING")
        self.assertEqual(
            result["bot"]["configs"]["Alpha"]["notify_group"],
            "10003",
        )

    def test_enabled_mode_alone_overrides_selected_profile(self):
        result = apply_panel_overrides(
            self._config(), {"enabled_mode": "disabled"}
        )

        self.assertIs(
            result["bot"]["configs"]["Alpha"]["settings"]["enabled"], False
        )

    def test_invalid_panel_modes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "repo_mode"):
            apply_panel_overrides(self._config(), {"repo_mode": "maybe"})

        with self.assertRaisesRegex(ValueError, "archive_match_mode"):
            apply_panel_overrides(
                self._config(), {"archive_match_mode": "everything"}
            )

    def test_missing_selected_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "指定配置不存在"):
            apply_panel_overrides(
                self._config(),
                {"default_config": "Missing"},
            )


class EffectiveConfigLoadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin_data = Path(self.temp.name) / "plugin-data"
        self.plugin_data.mkdir()
        self.config_path = self.plugin_data / "custom-config.json"
        self.config_path.write_text(
            json.dumps(default_config(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def test_panel_file_data_and_log_paths_take_explicit_priority(self):
        cfg, config_path, data_dir = load_effective_config(
            {
                "config_file": "custom-config.json",
                "data_dir": "plugin-runtime",
                "default_config": "默认",
                "listen_groups": ["777"],
                "log_level": "debug",
            },
            plugin_data_dir=self.plugin_data,
        )

        self.assertEqual(config_path, self.config_path.resolve())
        self.assertEqual(data_dir, (self.plugin_data / "plugin-runtime").resolve())
        self.assertEqual(cfg["bot"]["data_dir"], "plugin-runtime")
        self.assertEqual(
            cfg["bot"]["configs"]["默认"]["listen_groups"],
            ["777"],
        )
        self.assertEqual(cfg["log"]["log_level"], "DEBUG")

    def test_relative_configured_data_dir_is_relative_to_plugin_data(self):
        cfg = default_config()
        cfg["bot"]["data_dir"] = "state/maa"
        self.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False),
            encoding="utf-8",
        )

        effective, _, data_dir = load_effective_config(
            {"config_file": str(self.config_path)},
            plugin_data_dir=self.plugin_data,
        )

        self.assertEqual(effective["bot"]["data_dir"], "state/maa")
        self.assertEqual(data_dir, (self.plugin_data / "state/maa").resolve())

    def test_loader_creates_default_config_when_missing(self):
        new_path = self.plugin_data / "new-config.json"

        effective, config_path, data_dir = load_effective_config(
            {"config_file": str(new_path)},
            plugin_data_dir=self.plugin_data,
        )

        self.assertTrue(config_path.is_file())
        self.assertIn("bot", effective)
        self.assertEqual(data_dir, (self.plugin_data / "data").resolve())
