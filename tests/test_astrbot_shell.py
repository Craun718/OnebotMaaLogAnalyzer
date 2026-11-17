"""Tests for the AstrBot plugin shell without importing AstrBot itself."""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import get_origin
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import BaseModel, ValidationError

from maa_analyzer.config import (
    PANEL_DEFAULTS,
    PANEL_SCHEMA_GROUPS,
    PanelConfig,
    flatten_panel_config,
    setup_logging,
    teardown_logging,
)


ROOT = Path(__file__).resolve().parent.parent
SHELL_PATH = ROOT / "astrbot_plugin" / "main.py"


class _FakeStar:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config


class _RuntimeStub:
    def __init__(self, *, relevant=False):
        self.initialize = AsyncMock()
        self.terminate = AsyncMock()
        self.is_relevant_event = MagicMock(return_value=relevant)


def _load_shell(plugin_data_dir: Path):
    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = object

    class _CustomFilter:
        def __init__(self, raise_error=True):
            self.raise_error = raise_error

    class _Filter:
        CustomFilter = _CustomFilter

        class EventMessageType:
            ALL = "all"

        class PlatformAdapterType:
            AIOCQHTTP = "aiocqhttp"

        @staticmethod
        def event_message_type(_value):
            return lambda function: function

        @staticmethod
        def platform_adapter_type(_value):
            return lambda function: function

        @staticmethod
        def custom_filter(custom_filter_type, *_args, **_kwargs):
            def decorator(function):
                function.custom_filter_type = custom_filter_type
                return function

            return decorator

    event_module.filter = _Filter

    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = object
    star_module.Star = _FakeStar
    star_module.StarTools = SimpleNamespace(
        get_data_dir=lambda name: plugin_data_dir / name
    )

    api_module = types.ModuleType("astrbot.api")
    api_module.logger = MagicMock()
    api_module.event = event_module
    api_module.star = star_module
    astrbot_module = types.ModuleType("astrbot")
    astrbot_module.api = api_module

    spec = importlib.util.spec_from_file_location(
        "_onebot_maa_astrbot_shell_test",
        SHELL_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 AstrBot 插件入口")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "astrbot": astrbot_module,
            "astrbot.api": api_module,
            "astrbot.api.event": event_module,
            "astrbot.api.star": star_module,
        },
    ):
        spec.loader.exec_module(module)
    return module


class AstrBotPluginShellTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_root = Path(self.temp.name)
        self.shell = _load_shell(self.data_root)

    async def test_initialize_uses_plugin_data_dir_and_shared_runtime(self):
        fake_runtime = _RuntimeStub()
        factory = MagicMock(return_value=fake_runtime)
        config = {"listen_groups": ["123"]}

        with patch.object(
            self.shell,
            "AstrBotPluginRuntime",
            factory,
        ):
            plugin = self.shell.Main(object(), config)
            await plugin.initialize()
            await plugin.initialize()

        factory.assert_called_once()
        kwargs = factory.call_args.kwargs
        self.assertEqual(kwargs["plugin_data_dir"], self.data_root / "OnebotMaaLogAnalyzer")
        self.assertEqual(kwargs["template_root"], ROOT / "templates")
        runtime_config = factory.call_args.args[0]
        self.assertEqual(runtime_config["listen_groups"], ["123"])
        self.assertEqual(set(runtime_config), set(PANEL_DEFAULTS))
        self.assertEqual(config["basic"]["listen_groups"], ["123"])
        fake_runtime.initialize.assert_awaited()
        self.assertEqual(fake_runtime.initialize.await_count, 2)

    async def test_old_panel_markers_are_replaced_and_saved(self):
        class SavedConfig(dict):
            save_config = MagicMock()

        config = SavedConfig(history_mode="inherit", history_period_hours=-1,
                             repo_mode="disabled",
                             llm_models=[{"name": "default", "model": "custom-model"}])
        plugin = self.shell.Main(object(), config)
        self.assertEqual(config["history"]["history_mode"], "enabled")
        self.assertEqual(config["history"]["history_period_hours"], 24)
        self.assertEqual(config["repo_mode"], "disabled")
        self.assertEqual(config["llm"]["llm_models"],
                         [{"name": "default", "model": "custom-model"}])
        self.assertNotIn("history_mode", config)
        self.assertEqual(plugin.config["history_mode"], "enabled")
        self.assertEqual(plugin.config["history_period_hours"], 24)
        self.assertEqual(plugin.config["llm_models"],
                         [{"name": "default", "model": "custom-model"}])
        self.assertNotEqual(plugin.config, config)
        config.save_config.assert_called_once_with()

    async def test_old_llm_panel_is_rewritten_and_survives_reload(self):
        path = self.data_root / "plugin_config.json"

        class SavedConfig(dict):
            def save_config(self):
                path.write_text(json.dumps(self, ensure_ascii=False), encoding="utf-8")

        old = {"llm": {"llm_provider": "default", "llm_model": "first",
                       "llm_fallback_models": ["default:second"]}}
        path.write_text(json.dumps(old), encoding="utf-8")
        self.shell.Main(object(), SavedConfig(json.loads(path.read_text())))
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([item["model"] for item in saved["llm"]["llm_models"]],
                         ["first", "second"])
        self.assertEqual(saved["llm"]["llm_model"], "")
        plugin = self.shell.Main(object(), SavedConfig(saved))
        self.assertEqual([item["model"] for item in plugin.config["llm_models"]],
                         ["first", "second"])

    async def test_successful_runtime_finishes_pending_migration(self):
        class SavedConfig(dict):
            save_config = MagicMock()

        class MigratingRuntime:
            def __init__(self, panel, *, plugin_data_dir, template_root):
                self.panel = dict(panel)
                self.plugin_data_dir = plugin_data_dir

            async def initialize(self):
                from maa_analyzer.config import load_effective_config
                load_effective_config(self.panel, plugin_data_dir=self.plugin_data_dir)

        config = SavedConfig({"llm": {"llm_model": "custom-model"}})
        with patch.object(self.shell, "AstrBotPluginRuntime", MigratingRuntime):
            plugin = self.shell.Main(object(), config)
            self.assertTrue(config["llm"]["llm_migration_pending"])
            await plugin.initialize()
        path = self.data_root / "OnebotMaaLogAnalyzer" / "config.json"
        written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["bot"]["configs"]["默认"]["llm"]["models"][0]["model"],
                         "custom-model")
        self.assertFalse(config["llm"]["llm_migration_pending"])
        self.assertNotIn("_persist_llm_migration", plugin.config)
        self.assertEqual(config.save_config.call_count, 2)

    async def test_nested_panel_config_is_normalized_without_losing_unknown_fields(self):
        class SavedConfig(dict):
            save_config = MagicMock()

        config = SavedConfig({
            "basic": {
                "listen_groups": ["123"],
                "notify_group": "456",
                "advanced_note": "keep",
            },
            "vision": {"vision_mode": "disabled"},
            "llm": {"llm_models": [{"name": "default", "model": "custom-model"}]},
        })
        plugin = self.shell.Main(object(), config)

        self.assertEqual(plugin.config["listen_groups"], ["123"])
        self.assertEqual(plugin.config["notify_group"], "456")
        self.assertEqual(plugin.config["vision_mode"], "disabled")
        self.assertEqual(plugin.config["llm_models"],
                         [{"name": "default", "model": "custom-model"}])
        self.assertEqual(config["basic"]["advanced_note"], "keep")
        self.assertEqual(config["basic"]["listen_groups"], ["123"])
        self.assertIn("report", config)
        config.save_config.assert_called_once_with()

    async def test_blank_fixed_values_are_saved_back_to_webui(self):
        class SavedConfig(dict):
            def save_config(self):
                self.saved = dict(self)

        config = SavedConfig(PANEL_DEFAULTS)
        config.update(data_dir="", log_dir="", report_mode="",
                      report_device_scale_factor="", listen_groups=[],
                      llm_endpoint_api_key="", llm_profile_temperature="")
        plugin = self.shell.Main(object(), config)
        saved = flatten_panel_config(config.saved)
        for key in ("data_dir", "log_dir", "report_mode", "report_device_scale_factor"):
            self.assertEqual(saved[key], PANEL_DEFAULTS[key], key)
            self.assertEqual(saved[key], plugin.config[key], key)
        self.assertEqual(saved["listen_groups"], [])
        self.assertEqual(saved["llm_endpoint_api_key"], "")
        self.assertEqual(config.saved["llm_profile_temperature"], "")

    async def test_packaged_kernel_uses_plugin_namespace_and_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin_dir = Path(directory) / "test_plugin"
            shutil.copytree(ROOT / "astrbot_plugin", plugin_dir, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / "maa_analyzer", plugin_dir / "maa_analyzer", ignore=shutil.ignore_patterns("__pycache__"))
            package = "data.plugins.test_plugin"
            data = types.ModuleType("data")
            data.__path__ = []
            plugins = types.ModuleType("data.plugins")
            plugins.__path__ = [directory]
            api = types.ModuleType("astrbot.api")
            api.logger = MagicMock()
            event = types.ModuleType("astrbot.api.event")
            event.AstrMessageEvent = object
            event.filter = SimpleNamespace(
                CustomFilter=object,
                EventMessageType=SimpleNamespace(ALL=object()),
                PlatformAdapterType=SimpleNamespace(AIOCQHTTP=object()),
                event_message_type=lambda _: lambda f: f,
                platform_adapter_type=lambda _: lambda f: f,
                custom_filter=lambda _: lambda f: f,
            )
            star = types.ModuleType("astrbot.api.star")
            star.Context, star.Star, star.StarTools = object, _FakeStar, object
            modules = {"data": data, "data.plugins": plugins,
                       "astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
                       "astrbot.api.event": event, "astrbot.api.star": star}
            old_kernel = sys.modules["maa_analyzer"]
            with patch.dict(sys.modules, modules):
                first = importlib.import_module(package + ".main")
                analyzer_name = package + ".maa_analyzer.features.maa.analyzer"
                first_analyzer = sys.modules[analyzer_name].MaaAnalyzer
                self.assertTrue(first.AstrBotPluginRuntime.__module__.startswith(package + "."))
                self.assertIs(sys.modules["maa_analyzer"], old_kernel)
                for name in list(sys.modules):
                    if name == package or name.startswith(package + "."):
                        del sys.modules[name]
                second = importlib.import_module(package + ".main")
                self.assertIsNot(first_analyzer, sys.modules[analyzer_name].MaaAnalyzer)
                self.assertIsNot(first.AstrBotPluginRuntime, second.AstrBotPluginRuntime)

    async def test_terminate_releases_runtime_and_is_idempotent(self):
        fake_runtime = _RuntimeStub()
        with patch.object(
            self.shell,
            "AstrBotPluginRuntime",
            MagicMock(return_value=fake_runtime),
        ):
            plugin = self.shell.Main(object(), {})
            await plugin.initialize()
            await plugin.terminate()
            await plugin.terminate()

        fake_runtime.terminate.assert_awaited_once()
        self.assertIsNone(plugin.plugin_runtime)

    async def test_invalid_config_keeps_shell_available_without_activating_runtime(self):
        fake_runtime = _RuntimeStub(relevant=True)
        fake_runtime.initialize.side_effect = ValueError("端点不存在")
        with patch.object(self.shell, "AstrBotPluginRuntime", return_value=fake_runtime):
            plugin = self.shell.Main(object(), {})
            with self.assertLogs("Maa.AstrBot", level="ERROR") as logs:
                await plugin.initialize()
            self.assertIn("端点不存在", logs.output[0])
            self.assertFalse(self.shell.MaaEventFilter().filter(object(), {}))
            await plugin.terminate()
        fake_runtime.terminate.assert_awaited_once()

    async def test_astrbot_receives_kernel_logs_without_console_duplicate(self):
        fake_runtime = _RuntimeStub()
        target = logging.getLogger("Maa")
        old_level, old_propagate = target.level, target.propagate
        with patch.object(self.shell, "AstrBotPluginRuntime", return_value=fake_runtime), \
             patch.object(self.shell, "astrbot_logger") as astrbot_log:
            plugin = self.shell.Main(object(), {})
            try:
                await plugin.initialize()
                setup_logging({"log_to_file": False}, isolated=True)
                logging.getLogger("Maa.AstrBot").info("bridge test")
                astrbot_log.info.assert_called_once()
                self.assertIn("bridge test", astrbot_log.info.call_args.args[0])
                self.assertFalse(any(getattr(h, "_maa_plugin_handler", False) for h in target.handlers))
            finally:
                teardown_logging(isolated=True)
                await plugin.terminate()
                target.setLevel(old_level)
                target.propagate = old_propagate

    async def test_active_runtime_registry_is_cleared_on_terminate(self):
        fake_runtime = _RuntimeStub(relevant=True)
        with patch.object(
            self.shell,
            "AstrBotPluginRuntime",
            MagicMock(return_value=fake_runtime),
        ):
            plugin = self.shell.Main(object(), {})
            await plugin.initialize()
            event_filter = self.shell.MaaEventFilter()
            self.assertTrue(event_filter.filter(object(), {}))

            await plugin.terminate()

        self.assertFalse(event_filter.filter(object(), {}))
        self.assertEqual(list(self.shell._ACTIVE_RUNTIMES), [])

    async def test_custom_filter_rejects_unrelated_events(self):
        fake_runtime = _RuntimeStub()
        self.shell._ACTIVE_RUNTIMES.add(fake_runtime)
        self.addCleanup(self.shell._ACTIVE_RUNTIMES.discard, fake_runtime)

        self.assertFalse(self.shell.MaaEventFilter().filter(object(), {}))

    async def test_consumed_event_stops_astrbot_propagation(self):
        plugin = self.shell.Main(object(), {})
        plugin.plugin_runtime = SimpleNamespace(
            handle_event=AsyncMock(return_value=True)
        )
        event = SimpleNamespace(stop_event=MagicMock())

        await plugin.on_aiocqhttp_event(event)

        event.stop_event.assert_called_once_with()

    async def test_unrelated_event_is_ignored(self):
        plugin = self.shell.Main(object(), {})
        plugin.plugin_runtime = SimpleNamespace(
            handle_event=AsyncMock(return_value=False)
        )
        event = SimpleNamespace(stop_event=MagicMock())

        await plugin.on_aiocqhttp_event(event)

        event.stop_event.assert_not_called()

    async def test_handler_exception_does_not_propagate_to_astrbot(self):
        plugin = self.shell.Main(object(), {})
        plugin.plugin_runtime = SimpleNamespace(
            handle_event=AsyncMock(side_effect=RuntimeError("bad event"))
        )
        event = SimpleNamespace(stop_event=MagicMock())

        await plugin.on_aiocqhttp_event(event)

        event.stop_event.assert_not_called()


class AstrBotPackageMetadataTests(unittest.TestCase):
    def test_panel_sections_are_typed_pydantic_models(self):
        self.assertEqual(
            tuple(PanelConfig.model_fields),
            tuple(PANEL_SCHEMA_GROUPS),
        )
        for group, spec in PANEL_SCHEMA_GROUPS.items():
            self.assertTrue(issubclass(spec.section_model, BaseModel), group)
            self.assertEqual(
                spec.fields,
                tuple(spec.section_model.model_fields),
                group,
            )
            self.assertEqual(
                spec.section_model.model_config["extra"],
                "forbid",
                group,
            )

        commands_model = PANEL_SCHEMA_GROUPS["commands"].section_model
        with self.assertRaises(ValidationError):
            commands_model(commands_read_level="not-an-int")
        with self.assertRaises(ValidationError):
            commands_model(not_a_panel_field=True)

    def test_metadata_schema_and_requirements_are_plugin_ready(self):
        metadata = (ROOT / "astrbot_plugin" / "metadata.yaml").read_text(
            encoding="utf-8"
        )
        schema = json.loads(
            (ROOT / "astrbot_plugin" / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        requirements = (
            ROOT / "astrbot_plugin" / "requirements.txt"
        ).read_text(encoding="utf-8")

        for field in ("name", "desc", "version", "author"):
            self.assertRegex(metadata, rf"(?m)^{field}:\s*\S+")
        self.assertIn("name: OnebotMaaLogAnalyzer", metadata)
        self.assertIn("support_platforms:\n  - aiocqhttp", metadata)
        self.assertIn('astrbot_version: ">=4.29.0,<5"', metadata)
        legacy = {"llm_provider", "llm_model", "llm_fallback_models"}
        self.assertEqual([k for k in schema if k not in legacy], list(PANEL_SCHEMA_GROUPS))
        for key in legacy:
            self.assertTrue(schema[key]["invisible"])
            self.assertTrue(schema["llm"]["items"][key]["invisible"])
        self.assertTrue(schema["llm"]["items"]["llm_migration_pending"]["invisible"])
        leaves = {}
        for group, spec in PANEL_SCHEMA_GROUPS.items():
            section = schema[group]
            self.assertEqual(section["type"], "object", group)
            self.assertEqual(section["description"], spec.title, group)
            self.assertEqual([k for k in section["items"]
                              if k not in legacy | {"llm_migration_pending"}], list(spec.fields), group)
            for key in spec.fields:
                self.assertNotIn(key, leaves, key)
                leaves[key] = section["items"][key]
        self.assertEqual(set(leaves), set(PANEL_DEFAULTS))
        for key, default in PANEL_DEFAULTS.items():
            self.assertEqual(
                leaves[key]["default"],
                default,
                f"schema default mismatch: {key}",
            )
            self.assertNotIn("inherit", leaves[key].get("options", []), key)
        for spec in PANEL_SCHEMA_GROUPS.values():
            for field_name, field in spec.section_model.model_fields.items():
                schema_type = leaves[field_name]["type"]
                if schema_type == "list":
                    self.assertIs(get_origin(field.annotation), list, field_name)
                elif schema_type == "int":
                    self.assertIs(field.annotation, int, field_name)
                elif schema_type == "string":
                    self.assertIs(field.annotation, str, field_name)
        self.assertEqual(leaves["listen_groups"]["type"], "list")
        self.assertIn("aiohttp", requirements)


if __name__ == "__main__":
    unittest.main()
