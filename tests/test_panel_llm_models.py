import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maa_analyzer.config import (apply_panel_overrides, default_config,
                                 load_effective_config, synchronize_panel_config)
from maa_analyzer.core.llm import ChatMessage, LLMError
from maa_analyzer.core.llm_pool import LLMPool
from maa_analyzer.core.models import ConfigLLM


class PanelLLMModelsTests(unittest.TestCase):
    def _default_config(self):
        return default_config()

    def test_llm_models_use_array_order_for_fallback(self):
        result = apply_panel_overrides(self._default_config(), {
            "default_config": "默认",
            "llm_models": [
                {"model": "deepseek-flash"},
                {"name": "backup", "model": "backup-model"},
            ],
        })
        profile = result["bot"]["configs"]["默认"]
        self.assertEqual(profile["llm"]["models"], [
            {"name": "default", "model": "deepseek-flash"},
            {"name": "backup", "model": "backup-model"},
        ])

    def test_empty_llm_models_preserve_existing_candidates(self):
        cfg = self._default_config()
        cfg["bot"]["configs"]["默认"]["llm"]["models"] = [
            {"name": "alpha", "model": "alpha-model"},
        ]
        result = apply_panel_overrides(cfg, {
            "default_config": "默认",
            "llm_models": [],
        })
        self.assertEqual(result["bot"]["configs"]["默认"]["llm"]["models"], [
            {"name": "alpha", "model": "alpha-model"},
        ])

    def test_upgrade_reconciliation_migration_and_effective_load(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "astrbot_plugin/_conf_schema.json").read_text())
        # Model AstrBot's documented recursive schema reconciliation.
        def reconcile(nodes, saved):
            return {key: (reconcile(node["items"], saved.get(key, {}))
                          if node["type"] == "object" else copy.deepcopy(saved.get(key, node.get("default"))))
                    for key, node in nodes.items()}

        for grouped in (True, False):
            with self.subTest(grouped=grouped), tempfile.TemporaryDirectory() as tmp:
                old = {"llm_provider": "default", "llm_model": "deepseek-flash",
                       "llm_fallback_models": ["default:deepseek-v4-pro"]}
                saved = reconcile(schema, {"llm": old} if grouped else old)
                runtime, changed = synchronize_panel_config(saved)
                self.assertTrue(changed)
                self.assertEqual(saved["llm"]["llm_models"], [
                    {"__template_key": "model", "name": "default", "model": "deepseek-flash"},
                    {"__template_key": "model", "name": "default", "model": "deepseek-v4-pro"},
                ])
                self.assertEqual(saved["llm"]["llm_model"], "")
                self.assertEqual(saved["llm_model"], "")
                self.assertFalse(synchronize_panel_config(saved)[1])
                cfg, _, _ = load_effective_config(runtime, plugin_data_dir=tmp)
                self.assertEqual(cfg["bot"]["configs"]["默认"]["llm"]["models"], [
                    {"name": "default", "model": "deepseek-flash"},
                    {"name": "default", "model": "deepseek-v4-pro"},
                ])
                self.assertTrue({"deepseek-flash", "deepseek-v4-pro"}.issubset(
                    {m["model"] for m in cfg["llm"]["providers"][0]["models"]}))
                persisted = json.loads((Path(tmp) / "config.json").read_text())
                self.assertEqual(persisted["bot"]["configs"]["默认"]["llm"]["models"],
                                 cfg["bot"]["configs"]["默认"]["llm"]["models"])

    def test_existing_json_is_backed_up_and_other_profiles_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            old = default_config()
            old["bot"]["configs"]["other"] = copy.deepcopy(old["bot"]["configs"]["默认"])
            path.write_text(json.dumps(old), encoding="utf-8")
            panel, _ = synchronize_panel_config({"llm": {"llm_model": "new-model"}})
            load_effective_config(panel, plugin_data_dir=tmp)
            saved = json.loads(path.read_text())
            backup = json.loads((Path(tmp) / "config.json.panel-migration.bak").read_text())
            self.assertEqual(backup, old)
            self.assertEqual(saved["bot"]["configs"]["other"], old["bot"]["configs"]["other"])
            self.assertEqual(saved["bot"]["configs"]["默认"]["llm"]["models"][0]["model"], "new-model")
            load_effective_config(panel, plugin_data_dir=tmp)
            self.assertEqual(json.loads((Path(tmp) / "config.json.panel-migration.bak").read_text()), old)

    def test_failed_validation_keeps_both_migration_state_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            old = default_config()
            path.write_text(json.dumps(old), encoding="utf-8")
            panel_config = {"llm": {"llm_provider": "missing", "llm_model": "model"}}
            runtime, _ = synchronize_panel_config(panel_config)
            with self.assertRaisesRegex(ValueError, "不存在的端点"):
                load_effective_config(runtime, plugin_data_dir=tmp)
            self.assertEqual(json.loads(path.read_text()), old)
            self.assertTrue(panel_config["llm"]["llm_migration_pending"])
            self.assertTrue(synchronize_panel_config(panel_config)[0]["_persist_llm_migration"])

    def test_new_array_wins_and_old_fields_cannot_resurrect(self):
        config = {"llm": {"llm_models": [{"__template_key": "model", "name": "default", "model": "new"}],
                          "llm_model": "old", "llm_fallback_models": ["default:old-backup"]}}
        runtime, _ = synchronize_panel_config(config)
        self.assertEqual(runtime["llm_models"][0]["model"], "new")
        config["llm"]["llm_models"] = []
        self.assertEqual(synchronize_panel_config(config)[0]["llm_models"], [])

    def test_legacy_auto_primary_uses_endpoint_model_or_requires_selection(self):
        config = {"llm": {"llm_model": "", "llm_endpoint_model": "selected",
                          "llm_fallback_models": ["default:backup"]}}
        runtime, _ = synchronize_panel_config(config)
        self.assertEqual(runtime["llm_models"][0]["model"], "selected")
        config = {"llm": {"llm_fallback_models": ["default:backup"]}}
        runtime, _ = synchronize_panel_config(config)
        with self.assertRaisesRegex(ValueError, "模型名"):
            apply_panel_overrides(default_config(), runtime)

    def test_invalid_legacy_fallback_is_not_erased(self):
        config = {"llm": {"llm_model": "main", "llm_fallback_models": ["broken"]}}
        before = copy.deepcopy(config)
        with self.assertRaisesRegex(ValueError, "llm_fallback_models"):
            synchronize_panel_config(config)
        self.assertEqual(config, before)


class PanelLLMFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_migrated_candidates_are_attempted_in_order(self):
        panel, _ = synchronize_panel_config({"llm": {
            "llm_model": "first", "llm_fallback_models": ["default:second"],
            "llm_endpoint_max_retries": 0,
        }})
        cfg = apply_panel_overrides(default_config(), panel)
        pool = LLMPool(cfg["llm"])
        profile = ConfigLLM.model_validate(cfg["bot"]["configs"]["默认"]["llm"])
        attempted = []

        async def chat(_messages, **kwargs):
            attempted.append(kwargs["model"])
            if kwargs["model"] == "first":
                raise LLMError("failed", kind="other")
            return ChatMessage(content="ok")

        with patch.object(pool.clients["default"], "chat_messages", side_effect=chat):
            result = await pool.chat(profile, [], use_breaker=False)
        self.assertEqual(result.content, "ok")
        self.assertEqual(attempted, ["first", "second"])
