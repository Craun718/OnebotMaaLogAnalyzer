"""历史用量查询：完整文件读取与累计缓存命中率。"""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from maa_analyzer.core.ai_usage import UsageRecord, UsageStore
from maa_analyzer.core.llm import ChatMessage, LLMError
from maa_analyzer.core.llm_pool import LLMPool, session_context
from maa_analyzer.core.models import ConfigLLM
from maa_analyzer.features.maa.commands import CommandHandler


class AIUsageCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = UsageStore(self.tmp.name)
        self.handler = CommandHandler(SimpleNamespace(dm=SimpleNamespace(_dir=self.tmp.name)))

    async def test_all_reads_old_records_and_weights_cache_by_tokens(self):
        self.store.record(UsageRecord(
            session_id="old", prompt_tokens=900, completion_tokens=50,
            uncached_prompt_tokens=100, cache_read_tokens=800, elapsed_ms=1000,
        ))
        self.store.record(UsageRecord(
            session_id="new", prompt_tokens=100, completion_tokens=50,
            uncached_prompt_tokens=100, elapsed_ms=2000,
        ))
        # 缩小尾部窗口，确保普通查询只能看到最后一条，all 仍读取全文件。
        last_line_size = len(self.store.path.read_bytes().splitlines(keepends=True)[-1])
        with patch("maa_analyzer.core.ai_usage._MAX_READ_BYTES", last_line_size):
            recent = await self.handler._cmd_ai(None, [])
            result = await self.handler._cmd_ai(None, ["all"])
        self.assertIn("最近 1 个会话", recent.text)
        self.assertIn("全部历史 2 个会话 · 共 2 次调用", result.text)
        self.assertIn("输入 1.0k · 输出 100 · 合计 1.1k tokens", result.text)
        self.assertIn("缓存命中率：80.0%", result.text)
        self.assertIn("累计用时 3.0s", result.text)
        self.assertNotIn("全部成功", result.text)

    async def test_all_merges_followups_and_counts_failures_and_legacy_records(self):
        for record in (
            UsageRecord(session_id="same", prompt_tokens=10),
            UsageRecord(session_id="same", prompt_tokens=20),
            UsageRecord(session_id="same", ok=False),
            UsageRecord(prompt_tokens=30),
        ):
            self.store.record(record)
        with self.store.path.open("a") as handle:
            handle.write("broken json\n")
        result = await self.handler._cmd_ai(None, ["ALL"])
        self.assertIn("全部历史 2 个会话 · 共 4 次调用", result.text)
        self.assertIn("失败 1 次", result.text)
        self.assertIn("合计 60 tokens", result.text)
        self.assertIn("缓存命中率：暂无缓存数据", result.text)

    async def test_empty_history_and_invalid_argument(self):
        result = await self.handler._cmd_ai(None, ["all"])
        self.assertEqual(result.text, "还没有模型调用记录。")
        invalid = await self.handler._cmd_ai(None, ["invalid"])
        self.assertFalse(invalid.ok)
        self.assertIn("会话数|all", invalid.text)

    async def test_all_groups_by_group_id_across_configs_and_sessions(self):
        for record in (
            UsageRecord(session_id="a", group_id="111", config="one",
                        prompt_tokens=100, uncached_prompt_tokens=20, cache_read_tokens=80),
            UsageRecord(session_id="a", group_id="111", prompt_tokens=100,
                        uncached_prompt_tokens=100, phase="followup"),
            UsageRecord(session_id="b", group_id="111", config="two", ok=False),
            UsageRecord(session_id="c", group_id="222", config="one", prompt_tokens=50),
            UsageRecord(session_id="legacy", prompt_tokens=10),
            # 同一会话后来补有群号，可将较早无群号的调用归入该群。
            UsageRecord(session_id="legacy", group_id="222", prompt_tokens=10),
            UsageRecord(prompt_tokens=5),
        ):
            self.store.record(record)
        result = await self.handler._cmd_ai(None, ["all"])
        self.assertIn("群 111 · 2 个会话 · 3 次调用 · 失败 1 次", result.text)
        self.assertIn("群 222 · 2 个会话 · 3 次调用 · 失败 0 次", result.text)
        self.assertIn("未知群（记录无群号） · 1 个会话 · 1 次调用", result.text)
        group_one = result.text.split("群 111", 1)[1].split("群 222", 1)[0]
        self.assertIn("合计 200 tokens", group_one)
        self.assertIn("缓存命中率：40.0%", group_one)
        self.assertLess(result.text.index("群 111"), result.text.index("群 222"))

    async def test_pool_records_group_for_concurrent_calls_followups_and_failures(self):
        pool = LLMPool({"providers": [{
            "name": "main", "api_key": "test", "base_url": "https://example.test",
            "models": [{"model": "model"}],
        }]}, usage=self.store)
        config = ConfigLLM.model_validate({"models": [{"name": "main", "model": "model"}]})

        async def reply(*args, **kwargs):
            await asyncio.sleep(0)
            return ChatMessage(content="ok", usage={"prompt_tokens": 10})

        async def call(group, sid, phase):
            with session_context(sid, "config", "logs.zip", group):
                await pool.chat(config, [], phase=phase, use_breaker=False)

        with self.assertLogs("Maa.LLMPool", level="INFO") as logs, \
             patch.object(pool.clients["main"], "chat_messages", side_effect=reply):
            await asyncio.gather(call("111", "a", "analysis"), call("222", "b", "analysis"))
            await call("111", "a", "followup")
        self.assertTrue(any(
            "输入 10 · 输出 0 · 合计 10 tokens · 缓存命中率：暂无缓存数据" in line
            for line in logs.output
        ))
        with patch.object(pool.clients["main"], "chat_messages", new_callable=AsyncMock,
                          side_effect=LLMError("failed", kind="auth")):
            await call("222", "b", "followup")
        records = self.store._read_all(full_history=True)
        self.assertTrue(records)
        self.assertTrue(all(record.group_id == {"a": "111", "b": "222"}[record.session_id]
                            for record in records))
        self.assertTrue(any(not record.ok and record.group_id == "222" for record in records))
        self.assertTrue(any(record.phase == "followup" and record.ok for record in records))

    async def test_recent_default_and_limit_are_preserved(self):
        for index in range(25):
            self.store.record(UsageRecord(session_id=str(index), ts=index + 1))
        for args, expected in (([], 5), (["10"], 10), (["99"], 20)):
            with self.subTest(args=args):
                result = await self.handler._cmd_ai(None, args)
                self.assertIn(f"最近 {expected} 个会话", result.text)

    async def test_recent_shows_weighted_overall_usage_and_session_usage(self):
        self.store.record(UsageRecord(
            session_id="a", prompt_tokens=900, completion_tokens=20,
            uncached_prompt_tokens=100, cache_read_tokens=800,
        ))
        self.store.record(UsageRecord(
            session_id="b", prompt_tokens=100, completion_tokens=30,
            uncached_prompt_tokens=100,
        ))
        result = await self.handler._cmd_ai(None, [])
        self.assertIn(
            "输入 1.0k · 输出 50 · 合计 1.1k tokens · 缓存命中率：80.0%", result.text,
        )
        self.assertIn(
            "输入 900 · 输出 20 · 合计 920 tokens · 缓存命中率：88.9%", result.text,
        )
        self.assertIn(
            "输入 100 · 输出 30 · 合计 130 tokens · 缓存命中率：暂无缓存数据", result.text,
        )
