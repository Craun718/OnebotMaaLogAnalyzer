import unittest

from maa_analyzer.core.models import ProjectConfig


class KnownUIAlwaysEnabledTests(unittest.TestCase):
    def test_known_ui_ignores_legacy_switches_and_auto_fallback(self):
        project = ProjectConfig.model_validate({"ui": {
            "mxu": {"enabled": False},
            "mfaa": {"enabled": False},
            "maafwapp": {"enabled": False},
            "mta": {"enabled": False},
            "mwu": {"enabled": False},
            "auto": {"enabled": False},
        }})
        for key in (
            "mxu", "mxuapp", "mfaa", "mfaa-avalonia", "maafwapp", "maafw",
            "mta", "maatauriandroid", "mwu",
        ):
            with self.subTest(key=key):
                self.assertTrue(project.ui_enabled(key))
        self.assertFalse(project.ui_enabled("auto"))
        self.assertFalse(project.ui_enabled("unknown-ui"))
