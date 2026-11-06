"""Regression tests for the issues raised in the PR #14 review.

Each test names one reviewer point so a future change cannot silently
re-introduce the bug:

1. runtime.stop() called a nonexistent MaaAnalyzer.close().
2. The watcher dropped the file URL, so URL-only group files failed.
3. An empty panel config_file resolved to the plugin directory itself.
4. MaaAnalyzer ignored template_root and pointed at a wrong default path.
5. MessageHandler never reported that it consumed a message.
6. AstrBot treated a successful upload with data: None as a failure.
7. Periodic history cleanup used sync methods and skipped staging.
8. Every OneBot notice was mapped as group_upload.
9. analysis_concurrency still appeared in the panel schema and defaults.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from maa_analyzer.config import (
    PANEL_DEFAULTS,
    PANEL_PROFILE_INT_OVERRIDES,
    default_config,
    load_effective_config,
)
from maa_analyzer.core.models import AnalysisSettings, ConfigState, LogFileItem
from maa_analyzer.features.maa import analyzer as analyzer_module
from maa_analyzer.features.maa.analyzer import MaaAnalyzer
from maa_analyzer.features.maa.message_handler import MessageHandler
from maa_analyzer.features.maa.watcher import LogWatcher
from maa_analyzer.platforms.astrbot.adapter import (
    AstrBotPlatformAdapter,
    _api_success,
)
from maa_analyzer.platforms.onebot.handler import EventHandler
from maa_analyzer.runtime import MaaWorkflowRuntime


class _FakePort:
    """Minimal PlatformPort stand-in for lifecycle tests."""

    def __init__(self) -> None:
        self.closed = False

    async def send_text(self, group_id: int, text: str) -> str | None:
        return "fake-message-id"

    async def send_message(self, group_id, message):
        return "fake-message-id"

    async def send_image(self, group_id, image_bytes):
        return None

    async def send_file(self, group_id, file_name, data):
        return True

    async def upload_group_file(self, group_id, file, name):
        return True

    async def get_group_root_files(self, group_id, file_count=None):
        return {}

    async def get_group_files_by_folder(self, group_id, folder_id, file_count=None):
        return {}

    async def get_group_file_url(self, group_id, file_id):
        return None

    async def download_url(self, url, target_path, *, max_bytes, timeout_seconds=120):
        return 0

    async def get_version_info(self):
        return {}

    async def get_group_member_info(self, group_id, user_id):
        return {}

    async def close(self) -> None:
        self.closed = True


class _FakeLLM:
    """LLM stub that is configured, describable, and never owned."""

    configured = True

    def describe_all(self):
        return ["fake/endpoint"]


def _runtime(cfg: dict, data_dir: Path) -> MaaWorkflowRuntime:
    return MaaWorkflowRuntime(
        port=_FakePort(),
        cfg=cfg,
        data_dir=data_dir,
        llm=_FakeLLM(),
        background_services=False,
    )


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_then_stop_reaches_stopped_state(self):
        """Point 1: stop() must not die on the removed analyzer.close()."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(default_config(), Path(tmp))
            await runtime.start()

            self.assertTrue(runtime.running)
            self.assertIsNotNone(runtime.analyzer)
            self.assertFalse(hasattr(runtime.analyzer, "close"))

            await runtime.stop()

            self.assertEqual(runtime._state, "stopped")
            self.assertFalse(runtime.running)
            # The runtime does not own the port, so it must stay open.
            self.assertFalse(runtime.port.closed)

    async def test_history_cleanup_loop_uses_async_and_clears_staging(self):
        """Point 7: the periodic sweep must await both async cleanups."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(default_config(), Path(tmp))
            settings = AnalysisSettings()
            history = SimpleNamespace(
                cleanup_all_async=AsyncMock(return_value={}),
                cleanup_staging_async=AsyncMock(return_value=0),
            )
            service = SimpleNamespace(
                configs={"Alpha": SimpleNamespace(settings=settings)}
            )

            release = asyncio.Event()
            finished = asyncio.Event()
            sleeps = 0

            async def fake_sleep(_seconds):
                nonlocal sleeps
                sleeps += 1
                if sleeps == 1:
                    return
                await release.wait()

            async def staging_cleanup(*_args, **_kwargs):
                finished.set()
                return 0

            history.cleanup_staging_async.side_effect = staging_cleanup

            with patch("asyncio.sleep", new=fake_sleep):
                task = asyncio.create_task(
                    runtime._history_cleanup_loop(
                        history, service, interval_seconds=1
                    )
                )
                await asyncio.wait_for(finished.wait(), timeout=5)
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            history.cleanup_all_async.assert_awaited_once_with({"Alpha": settings})
            history.cleanup_staging_async.assert_awaited_once()
            self.assertGreaterEqual(sleeps, 2)


class PanelConfigFileTests(unittest.TestCase):
    """Point 3: an empty config_file must not resolve to the plugin directory."""

    def test_empty_config_file_falls_back_to_plugin_config_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_data = Path(tmp)
            effective, config_path, data_dir = load_effective_config(
                {"config_file": ""},
                plugin_data_dir=plugin_data,
            )

            self.assertEqual(
                config_path, (plugin_data / "config.json").resolve()
            )
            self.assertTrue(config_path.is_file())
            self.assertEqual(data_dir, (plugin_data / "data").resolve())
            self.assertIn("bot", effective)

    def test_missing_config_file_key_uses_the_same_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_data = Path(tmp)
            _, config_path, _ = load_effective_config(
                {}, plugin_data_dir=plugin_data
            )
            self.assertEqual(
                config_path, (plugin_data / "config.json").resolve()
            )


class ObsoleteAnalysisConcurrencyTests(unittest.TestCase):
    """Point 9: analysis runs serially per config, so the knob is gone."""

    def test_removed_from_panel_defaults_and_advanced_config(self):
        self.assertNotIn("settings_analysis_concurrency", PANEL_DEFAULTS)
        self.assertNotIn(
            "settings_analysis_concurrency", PANEL_PROFILE_INT_OVERRIDES
        )
        for profile in default_config()["bot"]["configs"].values():
            self.assertNotIn("analysis_concurrency", profile["settings"])
        self.assertFalse(hasattr(AnalysisSettings(), "analysis_concurrency"))
        self.assertGreaterEqual(AnalysisSettings().download_concurrency, 1)

    def test_removed_from_conf_schema(self):
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "astrbot_plugin"
            / "_conf_schema.json"
        )
        text = schema_path.read_text(encoding="utf-8")
        self.assertIn("settings_download_concurrency", text)
        self.assertNotIn("settings_analysis_concurrency", text)


class TemplateRootTests(unittest.TestCase):
    """Point 4: the injected root wins; the default is the repo templates dir."""

    def test_explicit_template_root_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "my-templates"
            analyzer = MaaAnalyzer(
                SimpleNamespace(), SimpleNamespace(), template_root=custom
            )
            self.assertEqual(analyzer._template_root, custom)

    def test_default_template_root_points_at_repository_templates(self):
        analyzer = MaaAnalyzer(SimpleNamespace(), SimpleNamespace())
        expected = Path(__file__).resolve().parents[1] / "templates"
        self.assertEqual(analyzer._template_root.resolve(), expected.resolve())
        self.assertTrue(expected.is_dir())


class WatcherFileUrlTests(unittest.IsolatedAsyncioTestCase):
    """Point 2: the watcher must forward the file URL to the analyzer."""

    async def test_forwards_url_when_no_file_id(self):
        captured = {}

        class _Analyzer:
            async def analyze(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(ok=True, error="")

        service = SimpleNamespace(send_text=AsyncMock())
        watcher = LogWatcher(service, _Analyzer())
        cfg = ConfigState(
            name="Alpha",
            settings=AnalysisSettings(
                send_summary=False, send_progress_message=False
            ),
        )
        file_info = LogFileItem(
            file_name="log.zip", url="https://example.invalid/log.zip", size=12
        )

        await watcher._analyze(cfg, "10001", "tester", file_info)

        self.assertEqual(captured["file_url"], "https://example.invalid/log.zip")
        self.assertEqual(captured["config_name"], "Alpha")
        service.send_text.assert_not_awaited()

    async def test_missing_id_and_url_reports_without_analyzing(self):
        calls = []

        class _Analyzer:
            async def analyze(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(ok=True, error="")

        service = SimpleNamespace(send_text=AsyncMock())
        watcher = LogWatcher(service, _Analyzer())
        cfg = ConfigState(name="Alpha", settings=AnalysisSettings(send_summary=False))
        file_info = LogFileItem(file_name="log.zip")

        await watcher._analyze(cfg, "10001", "tester", file_info)

        self.assertEqual(calls, [])
        service.send_text.assert_awaited_once()


class SerializedAnalysisFileUrlTests(unittest.IsolatedAsyncioTestCase):
    """Point 2: a URL alone is enough; the file_id lookup must be skipped."""

    class _Service:
        def __init__(self, data_dir: Path):
            self.dm = SimpleNamespace(_dir=str(data_dir))

        def add_job(self, cfg_name, **fields):
            return SimpleNamespace(**fields)

        def trim_jobs(self, cfg_name, keep=200):
            pass

        def schedule_save_config(self, cfg_name):
            pass

    async def _run(self, *, file_id, file_url):
        sentinel = RuntimeError("__download_reached__")
        download = AsyncMock(side_effect=sentinel)
        resolve = AsyncMock(return_value="resolved-id")

        with tempfile.TemporaryDirectory() as tmp:
            analyzer = MaaAnalyzer(self._Service(Path(tmp)), SimpleNamespace())
            analyzer._resolve_file_id = resolve
            with (
                patch.object(analyzer_module, "download_group_file", download),
                self.assertLogs(analyzer_module.logger, level="ERROR"),
            ):
                outcome = await analyzer._analyze_serialized(
                    config_name="Alpha",
                    settings=AnalysisSettings(file_list_delay_seconds=0),
                    group_id=10001,
                    file_name="log.zip",
                    file_id=file_id,
                    file_size=1024,
                    file_url=file_url,
                )
        return outcome, resolve, download

    async def test_url_only_skips_file_id_resolution(self):
        outcome, resolve, download = await self._run(
            file_id=None, file_url="  https://example.invalid/log.zip  "
        )

        resolve.assert_not_awaited()
        download.assert_awaited_once()
        self.assertEqual(download.await_args.args[2], None)
        self.assertEqual(
            download.await_args.kwargs["url"], "https://example.invalid/log.zip"
        )
        self.assertFalse(outcome.ok)
        self.assertIn("__download_reached__", outcome.error)

    async def test_no_url_falls_back_to_file_id_resolution(self):
        outcome, resolve, download = await self._run(file_id=None, file_url="")

        resolve.assert_awaited_once()
        download.assert_awaited_once()
        self.assertEqual(download.await_args.args[2], "resolved-id")
        self.assertFalse(outcome.ok)


class MessageHandlerConsumedTests(unittest.IsolatedAsyncioTestCase):
    """Point 5: the handler must report whether it consumed the message."""

    class _Service:
        def __init__(self, cfg):
            self._cfg = cfg
            self.sent: list[tuple[int, str]] = []

        def resolve_config(self, group_id):
            return self._cfg

        def has_pending_reload(self):
            return False

        async def send_text(self, group_id, text):
            self.sent.append((group_id, text))
            return "m1"

    def _handler(self, cfg, sessions=None):
        followup = SimpleNamespace(
            apply_policy=lambda name, settings: None,
            by_message_id=lambda mid, name: (sessions or {}).get(mid),
        )
        handler = MessageHandler(self._Service(cfg), SimpleNamespace(), followup)
        handler._send_chunks = AsyncMock()
        return handler

    def _event(self, text, *, group_id="10001", reply_id="", user_id="20002"):
        return {
            "group_id": group_id,
            "user_id": user_id,
            "_text": text,
            "_reply_id": reply_id,
            "sender": {"role": "member", "user_id": user_id},
        }

    async def test_matching_command_is_consumed(self):
        handler = self._handler(ConfigState(name="Alpha"))
        handler.commands.handle = AsyncMock(return_value=SimpleNamespace(text="ok"))

        consumed = await handler.on_message_text(self._event("/maa help"))

        self.assertTrue(consumed)
        handler.commands.handle.assert_awaited_once()

    async def test_valid_reference_with_followup_disabled_is_consumed(self):
        cfg = ConfigState(
            name="Alpha", settings=AnalysisSettings(followup_enabled=False)
        )
        handler = self._handler(cfg, {"777": SimpleNamespace(group_id="10001")})

        consumed = await handler.on_message_text(
            self._event("这为什么失败了", reply_id="777")
        )

        self.assertTrue(consumed)

    async def test_valid_reference_with_low_permission_is_consumed(self):
        cfg = ConfigState(name="Alpha")
        cfg.commands.followup_level = 2
        handler = self._handler(cfg, {"777": SimpleNamespace(group_id="10001")})

        consumed = await handler.on_message_text(
            self._event("这为什么失败了", reply_id="777")
        )

        self.assertTrue(consumed)
        self.assertTrue(handler.s.sent)
        self.assertIn("权限不足", handler.s.sent[0][1])

    async def test_ordinary_chat_is_not_consumed(self):
        handler = self._handler(ConfigState(name="Alpha"))

        consumed = await handler.on_message_text(self._event("今天天气不错"))

        self.assertFalse(consumed)

    async def test_unknown_or_cross_group_reference_is_not_consumed(self):
        sessions = {
            "777": SimpleNamespace(group_id="10001"),
            "888": SimpleNamespace(group_id="99999"),
        }
        handler = self._handler(ConfigState(name="Alpha"), sessions)

        self.assertFalse(
            await handler.on_message_text(self._event("?", reply_id="000"))
        )
        self.assertFalse(
            await handler.on_message_text(self._event("?", reply_id="888"))
        )

    async def test_empty_text_or_missing_group_is_not_consumed(self):
        handler = self._handler(ConfigState(name="Alpha"))

        self.assertFalse(await handler.on_message_text(self._event("   ")))
        self.assertFalse(
            await handler.on_message_text(self._event("hi", group_id=None))
        )


class AstrBotUploadSuccessTests(unittest.IsolatedAsyncioTestCase):
    """Point 6: an upload whose payload is null did not fail."""

    def test_api_success_still_flags_explicit_failures(self):
        self.assertTrue(
            _api_success({"status": "ok", "retcode": 0, "data": None})
        )
        self.assertFalse(_api_success({"status": "failed", "retcode": 1}))
        self.assertFalse(_api_success(None))
        self.assertFalse(_api_success({}))

    async def test_upload_group_file_accepts_null_payload(self):
        """Both response shapes mean success, so neither may retry."""
        for payload in (
            None,  # 实现只回传 data，而 upload_group_file 的 data 是 null
            {"status": "ok", "retcode": 0, "data": None},
        ):
            with self.subTest(payload=payload):
                bot = SimpleNamespace(call_action=AsyncMock(return_value=payload))
                adapter = AstrBotPlatformAdapter(bot)

                uploaded = await adapter.upload_group_file(
                    10001, "log.zip", "log.zip"
                )

                self.assertTrue(uploaded)
                bot.call_action.assert_awaited_once()

    async def test_upload_group_file_reports_real_failures(self):
        refused = SimpleNamespace(
            call_action=AsyncMock(
                return_value={"status": "failed", "retcode": 2, "data": None}
            )
        )
        self.assertFalse(
            await AstrBotPlatformAdapter(refused).upload_group_file(
                10001, "log.zip", "log.zip"
            )
        )

        raised = SimpleNamespace(
            call_action=AsyncMock(side_effect=RuntimeError("boom"))
        )
        with self.assertLogs("Maa.AstrBot", level="WARNING"):
            self.assertFalse(
                await AstrBotPlatformAdapter(raised).upload_group_file(
                    10001, "log.zip", "log.zip"
                )
            )

        self.assertFalse(
            await AstrBotPlatformAdapter(None).upload_group_file(
                10001, "log.zip", "log.zip"
            )
        )

    async def test_send_file_does_not_retry_with_base64(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = SimpleNamespace(call_action=AsyncMock(return_value=None))
            adapter = AstrBotPlatformAdapter(bot, outbox_dir=Path(tmp) / "outbox")

            uploaded = await adapter.send_file(10001, "report.pdf", b"pdf-bytes")

            self.assertTrue(uploaded)
            bot.call_action.assert_awaited_once()
            self.assertEqual(
                bot.call_action.await_args.args[0], "upload_group_file"
            )
            sent_file = str(bot.call_action.await_args.kwargs["file"])
            self.assertFalse(sent_file.startswith("base64://"))
            self.assertTrue(Path(sent_file).is_file())


class OneBotNoticeFilterTests(unittest.IsolatedAsyncioTestCase):
    """Point 8: only group_upload notices may reach the upload pipeline."""

    def _handler(self):
        adapter = SimpleNamespace(map_notice=Mock(return_value={"mapped": True}))
        service = SimpleNamespace(handle_notice=AsyncMock(return_value=True))
        return EventHandler(adapter, service), adapter, service

    async def test_non_upload_notice_is_ignored(self):
        handler, adapter, service = self._handler()

        for notice in ("group_increase", "group_ban", "notify", ""):
            consumed = await handler.on_notice(
                {"post_type": "notice", "notice_type": notice}
            )
            self.assertFalse(consumed, notice)

        adapter.map_notice.assert_not_called()
        service.handle_notice.assert_not_awaited()

    async def test_group_upload_notice_still_forwarded(self):
        handler, adapter, service = self._handler()

        consumed = await handler.on_notice(
            {"post_type": "notice", "notice_type": "group_upload"}
        )

        self.assertTrue(consumed)
        service.handle_notice.assert_awaited_once_with({"mapped": True})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
