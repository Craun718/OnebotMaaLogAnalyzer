"""Regression tests for the bare OneBot adapter and legacy configuration."""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from maa_analyzer.config import (
    default_config,
    load_config_file,
    validate_common_config,
    validate_onebot_config,
)
from maa_analyzer.core.data_manager import DataManager
from maa_analyzer.platform import GroupMessageEvent, GroupUploadNotice
from maa_analyzer.platforms.onebot.adapter import OneBotPlatformAdapter
from maa_analyzer.platforms.onebot.api import OneBotAPI
from main import _resolve_onebot_mode


class OneBotEventCompatibilityTests(unittest.TestCase):
    def test_maps_legacy_message_shape_and_normalizes_ids(self):
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 10001,
            "user_id": 20002,
            "message_id": 30003,
            "raw_message": " /maa status ",
            "sender": {
                "user_id": 20002,
                "role": "ADMIN",
                "nickname": "tester",
                "card": "card-name",
            },
            "message": [
                {"type": "text", "data": {"text": "/maa status"}},
                {"type": "reply", "data": {"id": 40004}},
                {
                    "type": "file",
                    "data": {
                        "id": 50005,
                        "name": "Maa-logs.zip",
                        "size": "not-a-number",
                        "busid": None,
                        "url": "https://example.test/logs.zip",
                    },
                },
            ],
        }

        mapped = OneBotPlatformAdapter.map_message(event)

        self.assertIsInstance(mapped, GroupMessageEvent)
        self.assertEqual(mapped.group_id, "10001")
        self.assertEqual(mapped.user_id, "20002")
        self.assertEqual(mapped.message_id, "30003")
        self.assertEqual(mapped.reply_id, "40004")
        self.assertEqual(mapped.text, "/maa status")
        self.assertEqual(mapped.sender.role, "admin")
        self.assertEqual(len(mapped.file_segments), 1)
        self.assertEqual(mapped.file_segments[0].file_id, "50005")
        self.assertEqual(mapped.file_segments[0].name, "Maa-logs.zip")
        self.assertEqual(mapped.file_segments[0].size, 0)
        self.assertEqual(mapped.file_segments[0].busid, 0)

    def test_maps_group_upload_notice_and_legacy_payload(self):
        event = {
            "post_type": "notice",
            "notice_type": "group_upload",
            "group_id": 12345,
            "user_id": 67890,
            "file": {
                "id": 24680,
                "name": "logs-part01.zip",
                "size": 4096,
                "busid": 2,
                "url": "https://example.test/part01.zip",
            },
        }

        mapped = OneBotPlatformAdapter.map_notice(event)

        self.assertIsInstance(mapped, GroupUploadNotice)
        self.assertEqual(mapped.group_id, "12345")
        self.assertEqual(mapped.user_id, "67890")
        self.assertEqual(mapped.file.file_id, "24680")
        payload = mapped.to_service_payload()
        self.assertEqual(payload["post_type"], "notice")
        self.assertEqual(payload["file"]["id"], "24680")
        self.assertEqual(payload["file"]["name"], "logs-part01.zip")


class OneBotOutboundCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_and_image_send_use_legacy_api_and_return_string_id(self):
        api = SimpleNamespace(
            send_group_msg=AsyncMock(
                side_effect=[
                    {"message_id": 101},
                    {"message_id": "102"},
                ]
            ),
            close=AsyncMock(),
        )
        adapter = OneBotPlatformAdapter(api)

        text_id = await adapter.send_text(12345, "hello")
        image_id = await adapter.send_image(12345, b"image-data")

        self.assertEqual(text_id, "101")
        self.assertEqual(image_id, "102")
        first_message = api.send_group_msg.await_args_list[0].args[1]
        self.assertEqual(first_message, "hello")
        second_message = api.send_group_msg.await_args_list[1].args[1]
        encoded = second_message[0]["data"]["file"].removeprefix("base64://")
        self.assertEqual(base64.b64decode(encoded), b"image-data")

    async def test_file_send_uses_local_upload_then_base64_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            api = SimpleNamespace(
                upload_group_file=AsyncMock(side_effect=[None, True]),
            )
            adapter = OneBotPlatformAdapter(api, outbox_dir=temp)

            uploaded = await adapter.send_file(
                12345, "../report.txt", b"report-data"
            )

        self.assertTrue(uploaded)
        self.assertEqual(api.upload_group_file.await_count, 2)
        first_args = api.upload_group_file.await_args_list[0].args
        second_args = api.upload_group_file.await_args_list[1].args
        self.assertEqual(first_args[0], 12345)
        self.assertEqual(Path(first_args[1]).name.rsplit("_", 1)[-1], "report.txt")
        self.assertEqual(first_args[2], "report.txt")
        self.assertEqual(second_args[0], 12345)
        encoded = second_args[1].removeprefix("base64://")
        self.assertEqual(base64.b64decode(encoded), b"report-data")
        self.assertEqual(second_args[2], "report.txt")

    async def test_file_queries_download_and_close_delegate_to_api(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "download.zip"
            api = SimpleNamespace(
                get_group_root_files=AsyncMock(return_value={"files": [1]}),
                get_group_files_by_folder=AsyncMock(return_value={"files": [2]}),
                get_group_file_url=AsyncMock(return_value="https://file.test/x"),
                download_url=AsyncMock(return_value=12),
                get_version_info=AsyncMock(return_value={"app_name": "NapCat"}),
                get_group_member_info=AsyncMock(return_value={"role": "owner"}),
                close=AsyncMock(),
            )
            adapter = OneBotPlatformAdapter(api)

            root = await adapter.get_group_root_files(12345, file_count=10)
            folder = await adapter.get_group_files_by_folder(
                12345, "folder", file_count=5
            )
            url = await adapter.get_group_file_url(12345, "file-id")
            size = await adapter.download_url(
                url or "", target, max_bytes=100, timeout_seconds=9
            )
            version = await adapter.get_version_info()
            member = await adapter.get_group_member_info(12345, 67890)
            await adapter.close()

        self.assertEqual(root, {"files": [1]})
        self.assertEqual(folder, {"files": [2]})
        self.assertEqual(url, "https://file.test/x")
        self.assertEqual(size, 12)
        self.assertEqual(version["app_name"], "NapCat")
        self.assertEqual(member["role"], "owner")
        api.get_group_root_files.assert_awaited_once_with(12345, file_count=10)
        api.get_group_files_by_folder.assert_awaited_once_with(
            12345, "folder", file_count=5
        )
        api.download_url.assert_awaited_once_with(
            "https://file.test/x",
            target,
            max_bytes=100,
            timeout_seconds=9,
        )
        api.close.assert_awaited_once()

    async def test_upload_success_is_boolean(self):
        api = SimpleNamespace(
            upload_group_file=AsyncMock(
                side_effect=[{"message": "accepted"}, None]
            )
        )
        adapter = OneBotPlatformAdapter(api)

        self.assertTrue(
            await adapter.upload_group_file(12345, "/tmp/a.zip", "a.zip")
        )
        self.assertFalse(
            await adapter.upload_group_file(12345, "/tmp/b.zip", "b.zip")
        )


class OneBotConfigCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_all_four_modes_keep_existing_config_shape(self):
        cfg = default_config()
        expected = {
            "ws": "ws",
            "http_ws": "http_ws",
            "ws_reverse": "ws_reverse",
            "http_ws_reverse": "http_ws_reverse",
        }

        for configured_mode, resolved_mode in expected.items():
            with self.subTest(mode=configured_mode):
                cfg["onebot"]["mode"] = configured_mode
                self.assertEqual(
                    _resolve_onebot_mode(cfg, debug_enabled=False),
                    resolved_mode,
                )

    def test_live_validation_rejects_bad_urls_but_debug_accepts_them(self):
        cfg = default_config()
        cfg["onebot"]["mode"] = "http_ws"
        cfg["onebot"]["http_url"] = "not-a-url"

        with self.assertRaisesRegex(ValueError, "不是合法的 http 地址"):
            validate_onebot_config(cfg)
        self.assertEqual(
            _resolve_onebot_mode(cfg, debug_enabled=True),
            "http_ws",
        )

        cfg["onebot"]["mode"] = "unknown"
        with self.assertRaisesRegex(ValueError, "未知模式"):
            _resolve_onebot_mode(cfg, debug_enabled=True)

    def test_existing_config_with_unknown_advanced_fields_still_loads(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            cfg = default_config()
            cfg["bot"]["configs"]["默认"]["project"]["future_field"] = {
                "kept": True
            }
            cfg["custom_section"] = {"kept": True}
            path.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            loaded = load_config_file(path)
            errors = validate_common_config(loaded)

        self.assertEqual(errors, [])
        self.assertEqual(
            loaded["bot"]["configs"]["默认"]["project"]["future_field"],
            {"kept": True},
        )
        self.assertEqual(loaded["custom_section"], {"kept": True})

    def test_existing_v1_data_files_remain_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            config_dir = data_dir / "默认"
            config_dir.mkdir()
            (config_dir / "groups.json").write_text(
                json.dumps(
                    {
                        "listen_groups": ["123456789"],
                        "notify_group": "987654321",
                    }
                ),
                encoding="utf-8",
            )
            (config_dir / "settings.json").write_text(
                json.dumps(
                    {
                        "max_report_images": 3,
                        "followup_enabled": False,
                    }
                ),
                encoding="utf-8",
            )
            (config_dir / "commands.json").write_text(
                json.dumps({"enabled": True, "prefix": "!maa"}),
                encoding="utf-8",
            )
            (data_dir / "settings.json").write_text(
                json.dumps({"max_report_images": 2}),
                encoding="utf-8",
            )

            manager = DataManager(str(data_dir))
            configs = manager.list_configs()
            info = manager.load_config_info("默认")
            old_settings = manager.load_config_settings("默认")
            commands = manager.load_config_commands("默认")

        self.assertEqual(configs, ["默认"])
        self.assertEqual(info.listen_groups, {"123456789"})
        self.assertEqual(info.notify_group, "987654321")
        self.assertEqual(old_settings["max_report_images"], 3)
        self.assertEqual(commands.prefix, "!maa")

    async def test_onebot_api_send_result_is_adapter_normalized(self):
        api = OneBotAPI()
        api._call = AsyncMock(return_value={"message_id": 54321})
        adapter = OneBotPlatformAdapter(api)

        message_id = await adapter.send_text(12345, "hello")

        self.assertEqual(message_id, "54321")


if __name__ == "__main__":
    unittest.main()
