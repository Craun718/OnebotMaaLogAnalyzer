"""AstrBot AIOCQHTTP adapter compatibility tests."""
from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from maa_analyzer.platform import GroupMessageEvent, GroupUploadNotice
from maa_analyzer.platforms.astrbot.adapter import AstrBotPlatformAdapter


def _message_event(raw: dict, *, role_fallback: bool = False):
    raw = dict(raw)
    raw.setdefault("post_type", "message")
    raw.setdefault("message_type", "group")
    sender = dict(raw.get("sender") or {})
    if role_fallback:
        sender.pop("role", None)
    raw["sender"] = sender
    message_obj = SimpleNamespace(
        raw_message=raw,
        message_id=raw.get("message_id"),
        sender=SimpleNamespace(
            user_id=sender.get("user_id"),
            role=sender.get("role"),
            nickname=sender.get("nickname", ""),
        ),
    )
    return SimpleNamespace(
        message_obj=message_obj,
        message_str="",
        get_group_id=lambda: raw.get("group_id"),
        get_sender_id=lambda: raw.get("user_id"),
        get_messages=lambda: [],
    )


def _upload_event(raw: dict):
    return SimpleNamespace(
        message_obj=SimpleNamespace(raw_message=raw),
        get_group_id=lambda: raw.get("group_id"),
        get_messages=lambda: [],
    )


class AstrBotEventMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_maps_group_text_file_reply_and_sender_ids(self):
        adapter = AstrBotPlatformAdapter()
        event = _message_event(
            {
                "group_id": 10001,
                "user_id": 20002,
                "message_id": 30003,
                "sender": {
                    "user_id": 20002,
                    "role": "admin",
                    "nickname": "tester",
                    "card": "card-name",
                },
                "message": [
                    {"type": "text", "data": {"text": " /maa status "}},
                    {"type": "reply", "data": {"id": 40004}},
                    {
                        "type": "file",
                        "data": {
                            "file_id": 50005,
                            "file_name": "Maa-logs.zip",
                            "size": 2048,
                            "busid": 6,
                            "url": "https://example.test/log.zip",
                        },
                    },
                ],
            }
        )

        mapped = await adapter.map_event(event)

        self.assertIsInstance(mapped, GroupMessageEvent)
        assert isinstance(mapped, GroupMessageEvent)
        self.assertEqual(mapped.group_id, "10001")
        self.assertEqual(mapped.user_id, "20002")
        self.assertEqual(mapped.message_id, "30003")
        self.assertEqual(mapped.reply_id, "40004")
        self.assertEqual(mapped.text, "/maa status")
        self.assertEqual(mapped.sender.role, "admin")
        self.assertEqual(mapped.sender.nickname, "tester")
        self.assertEqual(mapped.sender.card, "card-name")
        self.assertEqual(len(mapped.file_segments), 1)
        self.assertEqual(mapped.file_segments[0].file_id, "50005")
        self.assertEqual(mapped.file_segments[0].name, "Maa-logs.zip")
        self.assertEqual(mapped.file_segments[0].size, 2048)
        self.assertEqual(mapped.file_segments[0].busid, 6)

    async def test_maps_group_upload_notice(self):
        adapter = AstrBotPlatformAdapter()
        event = _upload_event(
            {
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
        )

        mapped = await adapter.map_event(event)

        self.assertIsInstance(mapped, GroupUploadNotice)
        assert isinstance(mapped, GroupUploadNotice)
        self.assertEqual(mapped.group_id, "12345")
        self.assertEqual(mapped.user_id, "67890")
        self.assertEqual(mapped.file.file_id, "24680")
        self.assertEqual(mapped.file.name, "logs-part01.zip")
        self.assertEqual(mapped.file.size, 4096)
        self.assertEqual(mapped.file.busid, 2)

    async def test_role_falls_back_to_member_api_and_is_cached(self):
        bot = SimpleNamespace(
            call_action=AsyncMock(
                return_value={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"role": "owner"},
                }
            )
        )
        adapter = AstrBotPlatformAdapter(bot=bot, role_cache_seconds=60)
        event = _message_event(
            {
                "group_id": 10001,
                "user_id": 20002,
                "message_id": 30003,
                "sender": {"user_id": 20002, "nickname": "tester"},
                "message": [{"type": "text", "data": {"text": "/maa help"}}],
            },
            role_fallback=True,
        )

        first = await adapter.map_event(event)
        second = await adapter.map_event(event)

        assert isinstance(first, GroupMessageEvent)
        assert isinstance(second, GroupMessageEvent)
        self.assertEqual(first.sender.role, "owner")
        self.assertEqual(second.sender.role, "owner")
        bot.call_action.assert_awaited_once()
        args, kwargs = bot.call_action.await_args
        self.assertEqual(args[0], "get_group_member_info")
        self.assertEqual(kwargs["group_id"], 10001)
        self.assertEqual(kwargs["user_id"], 20002)

    async def test_non_group_messages_and_private_messages_are_ignored(self):
        adapter = AstrBotPlatformAdapter()
        private = _message_event(
            {
                "message_type": "private",
                "user_id": 20002,
                "message": [{"type": "text", "data": {"text": "/maa help"}}],
            }
        )
        ungrouped = _message_event(
            {
                "group_id": None,
                "user_id": 20002,
                "message": [{"type": "text", "data": {"text": "/maa help"}}],
            }
        )

        self.assertIsNone(await adapter.map_event(private))
        self.assertIsNone(await adapter.map_event(ungrouped))


class AstrBotOutboundTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_text_uses_send_group_msg_and_extracts_message_id(self):
        bot = SimpleNamespace(
            send_group_msg=AsyncMock(
                return_value={"status": "ok", "data": {"message_id": 98765}}
            )
        )
        adapter = AstrBotPlatformAdapter(bot=bot)

        message_id = await adapter.send_text(12345, "hello")

        self.assertEqual(message_id, "98765")
        args, kwargs = bot.send_group_msg.await_args
        self.assertEqual(args, ())
        self.assertEqual(kwargs["group_id"], 12345)
        self.assertEqual(
            kwargs["message"],
            [{"type": "text", "data": {"text": "hello"}}],
        )

    async def test_send_text_falls_back_to_call_action(self):
        bot = SimpleNamespace(
            call_action=AsyncMock(
                return_value={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": "wrapped-id"},
                }
            )
        )
        adapter = AstrBotPlatformAdapter(bot=bot)

        message_id = await adapter.send_text(12345, "hello")

        self.assertEqual(message_id, "wrapped-id")
        args, kwargs = bot.call_action.await_args
        self.assertEqual(args[0], "send_group_msg")
        self.assertEqual(kwargs["group_id"], 12345)

    async def test_failed_response_returns_no_message_id(self):
        bot = SimpleNamespace(
            send_group_msg=AsyncMock(
                return_value={
                    "status": "failed",
                    "retcode": 100,
                    "data": {"message_id": "must-not-be-used"},
                }
            )
        )
        adapter = AstrBotPlatformAdapter(bot=bot)

        self.assertIsNone(await adapter.send_text(12345, "hello"))

    async def test_send_image_uses_base64_message_segment(self):
        bot = SimpleNamespace(
            send_group_msg=AsyncMock(return_value={"message_id": 101})
        )
        adapter = AstrBotPlatformAdapter(bot=bot)

        message_id = await adapter.send_image(12345, b"image-data")

        self.assertEqual(message_id, "101")
        message = bot.send_group_msg.await_args.kwargs["message"]
        self.assertEqual(message[0]["type"], "image")
        encoded = message[0]["data"]["file"].removeprefix("base64://")
        self.assertEqual(base64.b64decode(encoded), b"image-data")

    async def test_upload_group_file_accepts_success_and_rejects_failure(self):
        bot = SimpleNamespace(
            call_action=AsyncMock(
                side_effect=[
                    {"status": "ok", "retcode": 0, "data": {}},
                    {"status": "failed", "retcode": 200},
                ]
            )
        )
        adapter = AstrBotPlatformAdapter(bot=bot)

        self.assertTrue(
            await adapter.upload_group_file(12345, "/tmp/a.zip", "a.zip")
        )
        self.assertFalse(
            await adapter.upload_group_file(12345, "/tmp/b.zip", "b.zip")
        )

    async def test_send_file_falls_back_from_local_path_to_base64(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = AstrBotPlatformAdapter(
                bot=SimpleNamespace(),
                outbox_dir=directory,
            )
            adapter.upload_group_file = AsyncMock(side_effect=[False, True])

            result = await adapter.send_file(12345, "../report.zip", b"zip-data")

            self.assertTrue(result)
            self.assertEqual(adapter.upload_group_file.await_count, 2)
            first = adapter.upload_group_file.await_args_list[0].args
            second = adapter.upload_group_file.await_args_list[1].args
            self.assertEqual(Path(first[1]).name.split("_", 1)[1], "report.zip")
            self.assertEqual(first[2], "report.zip")
            self.assertTrue(second[1].startswith("base64://"))
            self.assertEqual(
                base64.b64decode(second[1].removeprefix("base64://")),
                b"zip-data",
            )

    async def test_group_file_queries_unwrap_data_envelopes(self):
        bot = SimpleNamespace(
            call_action=AsyncMock(
                side_effect=[
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"files": [{"id": "f1"}]},
                    },
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"url": "https://example.test/f1.zip"},
                    },
                ]
            )
        )
        adapter = AstrBotPlatformAdapter(bot=bot)

        listing = await adapter.get_group_root_files(12345, file_count=10)
        url = await adapter.get_group_file_url(12345, "f1")

        self.assertEqual(listing["files"][0]["id"], "f1")
        self.assertEqual(url, "https://example.test/f1.zip")


class _DownloadContent:
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self.chunks:
            yield chunk


class _DownloadResponse:
    def __init__(self, *, status: int = 200, chunks: list[bytes] | None = None):
        self.status = status
        self.headers = {
            "Content-Length": str(sum(len(chunk) for chunk in chunks or []))
        }
        self.content = _DownloadContent(chunks or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _DownloadSession:
    closed = False

    def __init__(self, response: _DownloadResponse):
        self.response = response

    def get(self, _url: str, *, timeout):
        self.timeout = timeout
        return self.response

    async def close(self):
        self.closed = True


class AstrBotDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_replaces_target_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "report.zip"
            target.parent.mkdir()
            target.write_bytes(b"old")
            session = _DownloadSession(
                _DownloadResponse(chunks=[b"new-", b"content"])
            )
            adapter = AstrBotPlatformAdapter()
            adapter._session = session

            written = await adapter.download_url(
                "https://example.test/report.zip",
                target,
                max_bytes=1024,
            )

            self.assertEqual(written, len(b"new-content"))
            self.assertEqual(target.read_bytes(), b"new-content")
            self.assertEqual(list(target.parent.glob("*.part")), [])
            self.assertEqual(list(target.parent.glob(".*.part")), [])

    async def test_failed_download_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.zip"
            target.write_bytes(b"keep-me")
            session = _DownloadSession(_DownloadResponse(status=500))
            adapter = AstrBotPlatformAdapter()
            adapter._session = session

            with self.assertRaisesRegex(RuntimeError, "HTTP"):
                await adapter.download_url(
                    "https://example.test/report.zip",
                    target,
                    max_bytes=1024,
                )

            self.assertEqual(target.read_bytes(), b"keep-me")

    async def test_size_limit_preserves_existing_target_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.zip"
            target.write_bytes(b"keep-me")
            session = _DownloadSession(
                _DownloadResponse(chunks=[b"too-large"])
            )
            adapter = AstrBotPlatformAdapter()
            adapter._session = session

            with self.assertRaisesRegex(RuntimeError, "大小限制"):
                await adapter.download_url(
                    "https://example.test/report.zip",
                    target,
                    max_bytes=4,
                )

            self.assertEqual(target.read_bytes(), b"keep-me")
            self.assertEqual(
                [path for path in target.parent.iterdir() if ".part" in path.name],
                [],
            )
