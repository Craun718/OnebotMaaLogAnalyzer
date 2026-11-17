"""Tests for release archive metadata handling."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_PATH = ROOT / ".github" / "scripts" / "package.py"


def _load_package_module():
    spec = importlib.util.spec_from_file_location("release_package", PACKAGE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载发布打包脚本")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AstrBotPackageVersionTests(unittest.TestCase):
    def setUp(self):
        self.package = _load_package_module()

    def test_metadata_version_matches_release(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = Path(temp_dir) / "metadata.yaml"
            metadata.write_text(
                "name: OnebotMaaLogAnalyzer\n"
                "version: 0.1.0\n"
                "astrbot_version: \">=4.29.0,<5\"\n",
                encoding="utf-8",
            )

            self.package.set_metadata_version(metadata, "0.2.0")

            content = metadata.read_text(encoding="utf-8")
            self.assertIn("version: '0.2.0'", content)
            self.assertIn("astrbot_version: \">=4.29.0,<5\"", content)

    def test_metadata_version_rejects_multiline_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = Path(temp_dir) / "metadata.yaml"
            metadata.write_text("version: 0.1.0\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "换行符"):
                self.package.set_metadata_version(metadata, "0.2.0\nmalicious: true")

    def test_metadata_version_requires_version_field(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = Path(temp_dir) / "metadata.yaml"
            metadata.write_text("name: OnebotMaaLogAnalyzer\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "version 字段"):
                self.package.set_metadata_version(metadata, "0.2.0")


if __name__ == "__main__":
    unittest.main()
