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
            "report_template": "dark",
            "project_name": " Beta Project ",
            "archive_match_mode": "any",
            "file_prefix": "MaaXXX-logs",
            "require_log_layout_mode": "disabled",
            "ui_mxu_mode": "disabled",
            "ui_mfaa_mode": "enabled",
            "ui_maafwapp_mode": "disabled",
            "ui_auto_mode": "enabled",
            "repo_mode": "enabled",
            "llm_provider": "default",
            "llm_model": "gpt-4o-mini",
            "vision_mode": "disabled",
            "send_images_mode": "disabled",
            "send_repo_images_mode": "enabled",
            "send_files_mode": "enabled",
            "followup_mode": "disabled",
            "followup_window_minutes": 0,
            "history_mode": "disabled",
            "history_period_hours": 12,
            "history_keep_periods": 3,
            "commands_mode": "disabled",
            "commands_read_level": 1,
            "commands_write_level": 1,
            "commands_followup_level": 2,
            "log_to_file_mode": "disabled",
            "log_dir": "runtime-logs",
        }

        result = apply_panel_overrides(cfg, panel)

        self.assertEqual(result["bot"]["configs"]["Alpha"], cfg["bot"]["configs"]["Alpha"])
        beta = result["bot"]["configs"]["Beta"]
        self.assertEqual(beta["listen_groups"], ["10001", "10002"])
        self.assertEqual(beta["notify_group"], "10003")
        self.assertIs(beta["settings"]["enabled"], True)
        self.assertIs(beta["settings"]["send_files"], True)
        self.assertEqual(beta["commands"]["prefix"], "!maa")
        self.assertEqual(beta["commands"]["owners"], ["1", "2"])
        self.assertEqual(beta["commands"]["admins"], ["3"])
        self.assertEqual(beta["report"]["render"], "image")
        self.assertEqual(beta["report"]["template"], "dark")
        self.assertEqual(beta["project"]["name"], "Beta Project")
        self.assertEqual(beta["project"]["archive_match_mode"], "any")
        self.assertEqual(beta["project"]["file_prefix"], "MaaXXX-logs")
        self.assertIs(beta["project"]["require_log_layout"], False)
        self.assertEqual(
            beta["project"]["ui"],
            {
                "mxu": {"enabled": False},
                "mfaa": {"enabled": True},
                "maafwapp": {"enabled": False},
                "auto": {"enabled": True},
            },
        )
        self.assertIs(beta["project"]["repo"]["enabled"], True)
        self.assertEqual(
            beta["llm"]["models"],
            [
                {"name": "default", "model": "gpt-4o-mini"},
                {"name": "backup", "model": "backup-model"},
            ],
        )
        self.assertIs(beta["vision"]["enabled"], False)
        self.assertIs(beta["settings"]["send_images"], False)
        self.assertIs(beta["settings"]["send_repo_images"], True)
        self.assertIs(beta["settings"]["send_files"], True)
        self.assertIs(beta["settings"]["followup_enabled"], False)
        self.assertEqual(beta["settings"]["followup_window_minutes"], 0)
        self.assertIs(beta["settings"]["history_enabled"], False)
        self.assertEqual(beta["settings"]["history_period_hours"], 12)
        self.assertEqual(beta["settings"]["history_keep_periods"], 3)
        self.assertIs(beta["commands"]["enabled"], False)
        self.assertEqual(beta["commands"]["read_level"], 1)
        self.assertEqual(beta["commands"]["write_level"], 1)
        self.assertEqual(beta["commands"]["followup_level"], 2)
        self.assertIs(result["log"]["log_to_file"], False)
        self.assertEqual(result["log"]["log_dir"], "runtime-logs")
        self.assertEqual(
            beta["project"]["custom_advanced"],
            {"keep": True},
        )

    def test_vision_report_and_command_advanced_fields_are_panel_overridable(self):
        result = apply_panel_overrides(
            self._config(),
            {
                "default_config": "Alpha",
                "vision_max_images": 2,
                "vision_max_image_kb": 512,
                "vision_detail": "low",
                "vision_attach_error_screenshots_mode": "disabled",
                "vision_attach_repo_templates_mode": "disabled",
                "vision_allow_model_request_mode": "enabled",
                "report_max_width_px": 900,
                "report_max_height_px": 3000,
                "report_max_pages": 10,
                "report_device_scale_factor": "1.5",
                "report_font_family": " Noto Sans CJK ",
                "report_browser_path": "/usr/bin/chromium",
                "report_reuse_browser_mode": "disabled",
                "report_render_timeout_ms": 12000,
                "report_embed_attachments_mode": "disabled",
                "report_attach_markdown_mode": "enabled",
                "report_fallback_to_text_mode": "disabled",
                "report_render_followup_mode": "disabled",
                "commands_reply_chunk_chars": 2000,
                "commands_max_list_items": 20,
            },
        )
        profile = result["bot"]["configs"]["Alpha"]
        self.assertEqual(profile["vision"]["max_images"], 2)
        self.assertEqual(profile["vision"]["max_image_kb"], 512)
        self.assertEqual(profile["vision"]["detail"], "low")
        self.assertIs(profile["vision"]["attach_error_screenshots"], False)
        self.assertIs(profile["vision"]["attach_repo_templates"], False)
        self.assertIs(profile["vision"]["allow_model_request"], True)
        self.assertEqual(profile["report"]["max_width_px"], 900)
        self.assertEqual(profile["report"]["max_height_px"], 3000)
        self.assertEqual(profile["report"]["max_pages"], 10)
        self.assertEqual(profile["report"]["device_scale_factor"], 1.5)
        self.assertEqual(profile["report"]["font_family"], "Noto Sans CJK")
        self.assertEqual(profile["report"]["browser_path"], "/usr/bin/chromium")
        self.assertIs(profile["report"]["reuse_browser"], False)
        self.assertEqual(profile["report"]["render_timeout_ms"], 12000)
        self.assertIs(profile["report"]["embed_attachments"], False)
        self.assertIs(profile["report"]["attach_markdown"], True)
        self.assertIs(profile["report"]["fallback_to_text"], False)
        self.assertIs(profile["report"]["render_followup"], False)
        self.assertEqual(profile["commands"]["reply_chunk_chars"], 2000)
        self.assertEqual(profile["commands"]["max_list_items"], 20)

    def test_invalid_advanced_float_and_mode_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "report_device_scale_factor"):
            apply_panel_overrides(
                self._config(),
                {"report_device_scale_factor": "not-a-number"},
            )
        with self.assertRaisesRegex(ValueError, "report_reuse_browser_mode"):
            apply_panel_overrides(
                self._config(),
                {"report_reuse_browser_mode": "sometimes"},
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

        with self.assertRaisesRegex(
            ValueError, "followup_window_minutes"
        ):
            apply_panel_overrides(
                self._config(), {"followup_window_minutes": -2}
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
