"""Tests for the AstrBot plugin shell without importing AstrBot itself."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from maa_analyzer.config import PANEL_DEFAULTS


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
        self.assertEqual(factory.call_args.args[0], config)
        fake_runtime.initialize.assert_awaited()
        self.assertEqual(fake_runtime.initialize.await_count, 2)

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
        self.assertEqual(set(schema), set(PANEL_DEFAULTS))
        for key, default in PANEL_DEFAULTS.items():
            self.assertEqual(
                schema[key]["default"],
                default,
                f"schema default mismatch: {key}",
            )
        self.assertEqual(schema["listen_groups"]["type"], "list")
        self.assertIn("aiohttp", requirements)


if __name__ == "__main__":
    unittest.main()
