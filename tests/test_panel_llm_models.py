import unittest

from maa_analyzer.config import apply_panel_overrides, default_config


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
