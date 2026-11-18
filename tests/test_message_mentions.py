"""Mention routing regressions shared by both platform adapters."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from maa_analyzer.core.service import MaaService
from maa_analyzer.platforms.astrbot.adapter import AstrBotPlatformAdapter
from maa_analyzer.platforms.onebot.adapter import OneBotPlatformAdapter


class MessageMentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mentions_filter_before_text_and_file_dispatch(self):
        for targets, allowed in [
            ([], True), ([222], False), ([111], True),
            ([222, 111], True), (["all"], True), ([222, "all"], True),
        ]:
            for shape in ("segments", "raw", "string"):
                with self.subTest(targets=targets, shape=shape):
                    service = object.__new__(MaaService)
                    service._dispatch_event = AsyncMock(return_value=True)
                    segments = [
                        {"type": "at", "data": {"qq": target}}
                        for target in targets
                    ] + [
                        {"type": "reply", "data": {"id": "report"}},
                        {"type": "text", "data": {"text": "/maa help"}},
                        {"type": "file", "data": {"id": "f1", "name": "logs.zip"}},
                    ]
                    raw = "".join(f"[CQ:at,qq={target}]" for target in targets)
                    raw += "[CQ:reply,id=report]/maa help[CQ:file,id=f1,name=logs.zip]"
                    event = {
                        "message_type": "group", "group_id": 1,
                        "self_id": 111, "raw_message": raw,
                        "message": segments if shape == "segments" else (
                            raw if shape == "string" else []
                        ),
                    }
                    self.assertEqual(await service.handle_message(event), allowed)
                    self.assertEqual(service._dispatch_event.await_count, 2 if allowed else 0)

    async def test_adapters_preserve_self_id_for_routing(self):
        for target, allowed in [(111, True), (222, False), ("all", True)]:
            raw = {
                "message_type": "group", "group_id": 1, "user_id": 2,
                "self_id": 111, "sender": {"role": "member"},
                "message": [
                    {"type": "at", "data": {"qq": target}},
                    {"type": "text", "data": {"text": "/maa help"}},
                ],
            }
            astr_event = SimpleNamespace(message_obj=SimpleNamespace(raw_message=raw))
            mapped_events = [
                OneBotPlatformAdapter.map_message(raw),
                await AstrBotPlatformAdapter().map_group_message(astr_event),
            ]
            for mapped in mapped_events:
                with self.subTest(target=target, adapter=type(mapped)):
                    self.assertIsNotNone(mapped)
                    self.assertEqual(mapped.self_id, "111")
                    service = object.__new__(MaaService)
                    service._dispatch_event = AsyncMock(return_value=True)
                    self.assertEqual(await service.handle_message(mapped), allowed)

    async def test_astrbot_uses_message_object_self_id_without_raw_self_id(self):
        event = SimpleNamespace(message_obj=SimpleNamespace(
            self_id=111,
            raw_message={
                "message_type": "group", "group_id": 1, "user_id": 2,
                "sender": {"role": "member"},
            },
        ))
        mapped = await AstrBotPlatformAdapter().map_group_message(event)
        self.assertEqual(mapped.self_id, "111")

    def test_plain_text_at_names_do_not_filter_messages(self):
        self.assertFalse(MaaService._mentions_only_others({
            "raw_message": "/maa help @someone", "self_id": 111,
        }))
