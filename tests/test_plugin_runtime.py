"""Lifecycle and reload tests for the AstrBot plugin bridge."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from maa_analyzer.astrbot.plugin_runtime import AstrBotPluginRuntime
from maa_analyzer.core.models import (
    AnalysisSettings,
    CommandConfig,
    ProjectConfig,
)
from maa_analyzer.platforms.astrbot.adapter import AstrBotPlatformAdapter


class _FakeService:
    def __init__(self):
        self.reload_loader = None
        self.runtime_reload = None

    def register_reload_loader(self, loader):
        self.reload_loader = loader

    def register_runtime_reload(self, reload):
        self.runtime_reload = reload


class _RelevanceService(_FakeService):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def resolve_config(self, group_id):
        return self.cfg if str(group_id) == "10001" else None


class _FakeAdapter:
    def __init__(self, outbox_dir):
        self.outbox_dir = Path(outbox_dir)
        self.closed = False
        self.role_cache_clears = 0

    def clear_role_cache(self):
        self.role_cache_clears += 1

    async def close(self):
        self.closed = True


class _FakeRuntime:
    def __init__(self, adapter, cfg, data_dir, template_root):
        self.port = adapter
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.template_root = Path(template_root)
        self.service = _FakeService()
        self.running = False
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self):
        self.start_calls += 1
        self.running = True
        return self

    async def stop(self, *, force=False):
        self.stop_calls += 1
        self.running = False


def _event(*segments, group_id=10001, message_str=""):
    raw = {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": 20002,
        "message_id": 30003,
        "sender": {"user_id": 20002, "role": "member"},
        "message": list(segments),
    }
    return SimpleNamespace(
        message_obj=SimpleNamespace(raw_message=raw),
        message_str=message_str,
        get_group_id=lambda: group_id,
        get_messages=lambda: [],
    )


def _upload_event(*, group_id=10001):
    raw = {
        "post_type": "notice",
        "notice_type": "group_upload",
        "group_id": group_id,
        "user_id": 20002,
        "file": {"id": "f1", "name": "logs.zip", "size": 10},
    }
    return SimpleNamespace(
        message_obj=SimpleNamespace(raw_message=raw),
        get_group_id=lambda: group_id,
        get_messages=lambda: [],
    )


class _FailStartRuntime(_FakeRuntime):
    async def start(self):
        self.start_calls += 1
        self.running = False
        raise RuntimeError("replacement failed")


class _FailRollbackRuntime(_FakeRuntime):
    async def start(self):
        self.start_calls += 1
        if self.start_calls > 1:
            self.running = False
            raise RuntimeError("rollback failed")
        self.running = True
        return self


class PluginRuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin_data = self.root / "plugin-data"
        self.plugin_data.mkdir()
        self.config_path = self.plugin_data / "config.json"
        self.config_path.write_text("{}", encoding="utf-8")
        self.old_cfg = {
            "log": {"log_to_file": False, "log_level": "INFO"},
            "bot": {"configs": {}},
        }
        self.new_cfg = {
            "log": {"log_to_file": False, "log_level": "DEBUG"},
            "bot": {"configs": {}},
        }

    def _runtime(self, loader, factory, *, adapter=None):
        adapter = adapter or _FakeAdapter(self.root / "outbox")
        return AstrBotPluginRuntime(
            {},
            plugin_data_dir=self.plugin_data,
            template_root=self.root / "templates",
            config_loader=loader,
            adapter_factory=lambda _outbox: adapter,
            runtime_factory=factory,
        )

    async def test_initialize_and_terminate_are_idempotent(self):
        loader = lambda panel, data_dir: (
            self.old_cfg,
            self.config_path,
            self.root / "old-data",
        )
        runtime = _FakeRuntime
        adapter = _FakeAdapter(self.root / "outbox")
        plugin = self._runtime(loader, runtime, adapter=adapter)

        with (
            patch(
                "maa_analyzer.astrbot.plugin_runtime.setup_logging"
            ) as setup,
            patch(
                "maa_analyzer.astrbot.plugin_runtime.teardown_logging"
            ) as teardown,
        ):
            await plugin.initialize()
            await plugin.initialize()
            first_runtime = plugin.runtime
            await plugin.terminate()
            await plugin.terminate()

        self.assertIsNotNone(first_runtime)
        self.assertEqual(first_runtime.start_calls, 1)
        self.assertEqual(first_runtime.stop_calls, 1)
        self.assertTrue(adapter.closed)
        setup.assert_called_once()
        teardown.assert_called_once_with(isolated=True)
        self.assertFalse(plugin.running)

    async def test_failed_initialize_closes_partial_resources(self):
        loader = lambda panel, data_dir: (
            self.old_cfg,
            self.config_path,
            self.root / "failed-data",
        )
        adapter = _FakeAdapter(self.root / "outbox")
        plugin = self._runtime(loader, _FailStartRuntime, adapter=adapter)

        with (
            patch("maa_analyzer.astrbot.plugin_runtime.setup_logging"),
            patch(
                "maa_analyzer.astrbot.plugin_runtime.teardown_logging"
            ) as teardown,
        ):
            with self.assertRaisesRegex(RuntimeError, "replacement failed"):
                await plugin.initialize()

        self.assertFalse(plugin.running)
        self.assertTrue(adapter.closed)
        teardown.assert_called_once_with(isolated=True)

    async def test_successful_reload_rebinds_callbacks_and_data_dir(self):
        calls = 0

        def loader(panel, data_dir):
            nonlocal calls
            calls += 1
            if calls == 1:
                return self.old_cfg, self.config_path, self.root / "old-data"
            return self.new_cfg, self.config_path, self.root / "new-data"

        created = []

        def factory(adapter, cfg, data_dir, template_root):
            runtime = _FakeRuntime(adapter, cfg, data_dir, template_root)
            created.append(runtime)
            return runtime

        adapter = _FakeAdapter(self.root / "outbox")
        plugin = self._runtime(loader, factory, adapter=adapter)

        with (
            patch("maa_analyzer.astrbot.plugin_runtime.setup_logging"),
            patch("maa_analyzer.astrbot.plugin_runtime.teardown_logging"),
        ):
            await plugin.initialize()
            ok, message = await plugin._reload_runtime(self.new_cfg)

        self.assertTrue(ok, message)
        self.assertEqual(len(created), 2)
        self.assertIs(plugin.runtime, created[1])
        self.assertFalse(created[0].running)
        self.assertTrue(created[1].running)
        self.assertEqual(adapter.outbox_dir, self.root / "new-data" / "_outbox")
        self.assertEqual(adapter.role_cache_clears, 1)
        self.assertEqual(
            created[1].service.runtime_reload.__self__,
            plugin,
        )
        self.assertEqual(
            created[1].service.runtime_reload.__func__,
            plugin._reload_runtime.__func__,
        )
        self.assertEqual(plugin.cfg, self.new_cfg)

    async def test_failed_reload_restores_old_runtime_data_and_logging(self):
        calls = 0

        def loader(panel, data_dir):
            nonlocal calls
            calls += 1
            if calls == 1:
                return self.old_cfg, self.config_path, self.root / "old-data"
            return self.new_cfg, self.config_path, self.root / "new-data"

        created = []

        def factory(adapter, cfg, data_dir, template_root):
            runtime_type = _FakeRuntime if not created else _FailStartRuntime
            runtime = runtime_type(adapter, cfg, data_dir, template_root)
            created.append(runtime)
            return runtime

        adapter = _FakeAdapter(self.root / "outbox")
        plugin = self._runtime(loader, factory, adapter=adapter)

        with (
            patch(
                "maa_analyzer.astrbot.plugin_runtime.setup_logging"
            ) as setup,
            patch("maa_analyzer.astrbot.plugin_runtime.teardown_logging"),
        ):
            await plugin.initialize()
            ok, message = await plugin._reload_runtime(self.new_cfg)

        self.assertFalse(ok)
        self.assertIn("已回退", message)
        self.assertIs(plugin.runtime, created[0])
        self.assertTrue(created[0].running)
        self.assertEqual(created[0].start_calls, 2)
        self.assertEqual(adapter.outbox_dir, self.root / "old-data" / "_outbox")
        self.assertEqual(setup.call_count, 3)
        self.assertEqual(setup.call_args_list[-1].args[0], self.old_cfg["log"])
        self.assertEqual(plugin.cfg, self.old_cfg)
        self.assertTrue(plugin.running)

    async def test_failed_rollback_marks_plugin_failed(self):
        calls = 0

        def loader(panel, data_dir):
            nonlocal calls
            calls += 1
            cfg = self.old_cfg if calls == 1 else self.new_cfg
            return cfg, self.config_path, self.root / f"data-{calls}"

        created = []

        def factory(adapter, cfg, data_dir, template_root):
            runtime_type = _FailRollbackRuntime if not created else _FailStartRuntime
            runtime = runtime_type(adapter, cfg, data_dir, template_root)
            created.append(runtime)
            return runtime

        plugin = self._runtime(loader, factory)
        with (
            patch("maa_analyzer.astrbot.plugin_runtime.setup_logging"),
            patch("maa_analyzer.astrbot.plugin_runtime.teardown_logging"),
        ):
            await plugin.initialize()
            ok, message = await plugin._reload_runtime(self.new_cfg)

        self.assertFalse(ok)
        self.assertIn("回退也失败", message)
        self.assertEqual(plugin._state, "failed")
        self.assertFalse(plugin.running)


class PluginEventDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_preserves_account_for_mapping_and_service(self):
        adapter = AstrBotPlatformAdapter()
        bot = SimpleNamespace(
            call_action=AsyncMock(return_value={"role": "member"}),
            send_group_msg=AsyncMock(return_value={"message_id": 42}),
        )
        event = _event({"type": "text", "data": {"text": "/maa help"}})
        event.bot = bot
        raw = event.message_obj.raw_message
        raw["self_id"] = 111
        raw["sender"].pop("role")

        async def handle(mapped):
            # Simulate an unrelated event rebinding the global fallback client.
            adapter.bind_bot(SimpleNamespace())
            return await adapter.send_text(int(mapped.group_id), "report")

        plugin = AstrBotPluginRuntime({}, plugin_data_dir=".", template_root="templates")
        plugin.adapter = adapter
        plugin.runtime = SimpleNamespace(
            running=True,
            service=SimpleNamespace(handle_platform_event=handle),
        )
        self.assertTrue(await plugin.handle_event(event))
        bot.call_action.assert_awaited_once_with(
            "get_group_member_info", group_id=10001, user_id=20002,
            no_cache=False, self_id=111,
        )
        bot.send_group_msg.assert_awaited_once_with(
            group_id=10001, self_id=111,
            message=[{"type": "text", "data": {"text": "report"}}],
        )
        self.assertIsNone(adapter._event_route.get())

    async def test_event_is_bound_and_dispatched_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = _FakeAdapter(root / "outbox")
            adapter.bind_bot = unittest.mock.Mock()
            adapter.map_event = AsyncMock(
                return_value=SimpleNamespace(group_id="1")
            )
            plugin = AstrBotPluginRuntime(
                {},
                plugin_data_dir=root,
                template_root=root / "templates",
                config_loader=lambda panel, data_dir: (
                    {"log": {}},
                    root / "config.json",
                    root / "data",
                ),
                adapter_factory=lambda outbox: adapter,
                runtime_factory=_FakeRuntime,
            )
            with (
                patch("maa_analyzer.astrbot.plugin_runtime.setup_logging"),
                patch("maa_analyzer.astrbot.plugin_runtime.teardown_logging"),
            ):
                await plugin.initialize()
                plugin.service.handle_platform_event = AsyncMock(
                    return_value=True
                )
                service = plugin.service
                event = SimpleNamespace(bot=object())

                consumed = await plugin.handle_event(event)
                await plugin.terminate()

        self.assertTrue(consumed)
        adapter.bind_bot.assert_called_once_with(event.bot)
        service.handle_platform_event.assert_awaited_once()

    async def test_unrelated_event_is_not_dispatched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = _FakeAdapter(root / "outbox")
            adapter.map_event = AsyncMock(return_value=None)
            plugin = AstrBotPluginRuntime(
                {},
                plugin_data_dir=root,
                template_root=root / "templates",
                config_loader=lambda panel, data_dir: (
                    {"log": {}},
                    root / "config.json",
                    root / "data",
                ),
                adapter_factory=lambda outbox: adapter,
                runtime_factory=_FakeRuntime,
            )
            with (
                patch("maa_analyzer.astrbot.plugin_runtime.setup_logging"),
                patch("maa_analyzer.astrbot.plugin_runtime.teardown_logging"),
            ):
                await plugin.initialize()
                plugin.service.handle_platform_event = AsyncMock()
                service = plugin.service

                consumed = await plugin.handle_event(SimpleNamespace(bot=None))
                await plugin.terminate()

        self.assertFalse(consumed)
        service.handle_platform_event.assert_not_awaited()


class PluginEventRelevanceTests(unittest.TestCase):
    def setUp(self):
        cfg = SimpleNamespace(
            name="测试",
            project=ProjectConfig(archive_match_mode="any"),
            settings=AnalysisSettings(),
            commands=CommandConfig(prefix="/scan"),
        )
        self.session = SimpleNamespace(group_id="10001", closed=False)
        self.followup = SimpleNamespace(
            by_message_id=lambda message_id, config_name: (
                self.session if message_id == "90009" else None
            )
        )
        runtime = SimpleNamespace(
            running=True,
            service=_RelevanceService(cfg),
            followup=self.followup,
        )
        self.plugin = AstrBotPluginRuntime(
            {},
            plugin_data_dir=".",
            template_root="templates",
        )
        self.plugin.adapter = AstrBotPlatformAdapter()
        self.plugin.runtime = runtime

    def test_only_analyzable_events_are_relevant(self):
        cases = [
            (
                "ordinary chat",
                _event({"type": "text", "data": {"text": "hello"}}),
                False,
            ),
            (
                "default prefix is not configured prefix",
                _event({"type": "text", "data": {"text": "/maa status"}}),
                False,
            ),
            (
                "custom command prefix",
                _event({"type": "text", "data": {"text": "/scan status"}}),
                True,
            ),
            (
                "log file segment",
                _event(
                    {
                        "type": "file",
                        "data": {"file_name": "logs.zip", "id": "f1"},
                    }
                ),
                True,
            ),
            (
                "known follow-up reply",
                _event(
                    {"type": "reply", "data": {"id": "90009"}},
                    {"type": "text", "data": {"text": "why?"}},
                ),
                True,
            ),
            (
                "unknown reply",
                _event(
                    {"type": "reply", "data": {"id": "99999"}},
                    {"type": "text", "data": {"text": "why?"}},
                ),
                False,
            ),
            (
                "unlistened group",
                _event(
                    {"type": "text", "data": {"text": "/scan status"}},
                    group_id=10002,
                ),
                False,
            ),
            (
                "private message",
                SimpleNamespace(
                    message_obj=SimpleNamespace(
                        raw_message={
                            "post_type": "message",
                            "message_type": "private",
                            "message": [
                                {
                                    "type": "text",
                                    "data": {"text": "/scan status"},
                                }
                            ],
                        }
                    ),
                    get_group_id=lambda: None,
                    get_messages=lambda: [],
                ),
                False,
            ),
        ]

        for label, event, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(
                    self.plugin.is_relevant_event(event),
                    expected,
                )

    def test_group_upload_notice_is_relevant(self):
        self.assertTrue(self.plugin.is_relevant_event(_upload_event()))


if __name__ == "__main__":
    unittest.main()
