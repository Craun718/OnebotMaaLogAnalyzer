"""Tests for the shared workflow lifecycle."""
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from maa_analyzer.runtime import MaaWorkflowRuntime


class WorkflowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_closes_owned_resources_after_start(self):
        port = AsyncMock()
        llm = AsyncMock()
        llm.configured = True
        llm.describe_all = lambda: []
        with tempfile.TemporaryDirectory() as data_dir:
            with patch("maa_analyzer.runtime.validate_common_config", return_value=[]):
                runtime = MaaWorkflowRuntime(
                    port=port, cfg={}, data_dir=data_dir, llm=llm,
                    owns_llm=True, owns_port=True, background_services=False,
                )
            await runtime.start()
            await runtime.stop()

            self.assertFalse(runtime.running)
            llm.close.assert_awaited_once()
            port.close.assert_awaited_once()
            await runtime.stop()
            llm.close.assert_awaited_once()
            port.close.assert_awaited_once()
